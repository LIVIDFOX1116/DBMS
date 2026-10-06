"""
The API layer both halves of the project share (project bible, Section 7.2).

Two modes, controlled by the TXGRAPH_API_MODE env var:
  - "mock" (default): serves api/mock_data.json. Your partner runs this from
    day one -- no Postgres, no GPU, no ingestion needed at all.
  - "live": queries the real Postgres schema.

The endpoint shapes are IDENTICAL in both modes -- that's the whole point.
Switching TXGRAPH_API_MODE=live is meant to be a data-source swap for the
frontend, not a rebuild (see the bible's Section 7.3).

Run it:
    uvicorn api.main:app --reload --port 8000
    curl localhost:8000/wallets
    curl localhost:8000/docs      # interactive OpenAPI docs, free from FastAPI
"""
import json
import logging
import os
import pathlib
import secrets
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg2 import InterfaceError, OperationalError
from psycopg2 import pool as pg_pool
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from ingestion import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api.main")

# Lazy: created on first real (live-mode) query, not at import/startup time --
# mock mode (the default, and everything CI runs) never touches Postgres at
# all, and eagerly connecting here would turn that into a hard requirement.
# _pool_lock guards the lazy init itself: FastAPI dispatches sync endpoints
# to a worker thread pool, so a burst of concurrent first requests (e.g. the
# dashboard's initial batch of GET calls on load) can otherwise all observe
# `_pool is None` at once and each construct their own ThreadedConnectionPool
# -- whichever one loses the race to the global still has requests in flight
# against it, and returning their connections via the *other* (currently
# global) pool instance raises psycopg2.pool.PoolError: trying to put
# unkeyed connection. Reproduced live via a real `cargo tauri dev` launch.
_pool: "pg_pool.ThreadedConnectionPool | None" = None
_pool_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _pool
    if _pool is not None:
        _pool.closeall()
        log.info("Postgres connection pool closed")
        _pool = None


app = FastAPI(
    title="Transaction Graph Analyzer API",
    version="0.1.0",
    description="Shared contract between the GPU/backend half and the frontend half. See docs/API_CONTRACT.md.",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # HTTPException (404s, 422s, the 403 on /internal/api-key) already has
    # its own correct handling -- only step in for genuinely unexpected
    # errors (a DB error, a bug), and never leak DB error text, SQL, or a
    # traceback into the response body. The real detail goes to the log
    # (findable via the Tauri get_backend_logs command in a packaged app,
    # not just a dev terminal), not to the caller.
    if isinstance(exc, StarletteHTTPException):
        return await http_exception_handler(request, exc)
    log.exception("Unhandled error handling %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# No auth and no mutating endpoints (see API_CONTRACT.md "Known simplifications"),
# so a wildcard origin doesn't widen the attack surface -- it just lets the
# frontend be opened as a static file or served from any local dev port
# without per-origin config.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

MOCK_PATH = pathlib.Path(__file__).parent / "mock_data.json"


def using_mock() -> bool:
    return os.getenv("TXGRAPH_API_MODE", "mock").lower() != "live"


def load_mock() -> dict:
    return json.loads(MOCK_PATH.read_text())


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check: another thread may have won the race while we waited for the lock
                _pool = pg_pool.ThreadedConnectionPool(minconn=1, maxconn=10, **config.PG_DSN)
                log.info("Postgres connection pool created (min=1, max=10)")
    return _pool


@contextmanager
def get_conn():
    """Checks out a pooled connection. ThreadedConnectionPool, not
    SimpleConnectionPool: FastAPI runs sync endpoints (every endpoint in
    this file) in a worker thread pool, not the event loop thread, so the
    pool itself must be thread-safe. A connection that errors with a
    connection-level exception (OperationalError/InterfaceError -- e.g. the
    Postgres server restarting mid-request) is discarded (closed) rather
    than returned to the pool for reuse, so one broken connection can't
    poison every request after it."""
    conn = _get_pool().getconn()
    broken = False
    try:
        yield conn
    except (OperationalError, InterfaceError):
        broken = True
        raise
    finally:
        pool = _get_pool()
        if broken or conn.closed:
            pool.putconn(conn, close=True)
        else:
            pool.putconn(conn)


def rows_as_dicts(cur) -> list:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/health")
def health():
    return {"status": "ok", "mode": "mock" if using_mock() else "live"}


# ── Etherscan key handoff (desktop-app packaging, Part 2) ──────────────────
# The Rust shell owns the key in the OS keychain and never sends it to the
# webview -- it hands the key to *this* process once, as an env var, at
# spawn time. Held in memory only from here on: never written to disk, never
# logged, never echoed back. This is deliberately separate from
# ingestion/config.py's ETHERSCAN_API_KEY (.env-sourced) -- that constant
# stays exactly as-is for direct CLI/dev use (`python -m ingestion...`,
# scripts/run_pipeline.py); the two are unrelated and never synced.
#
# TXGRAPH_INTERNAL_TOKEN is a random value Rust generates fresh per app
# launch and passes at spawn alongside the key -- /internal/api-key requires
# it on every call, so no other local process can silently overwrite the key
# this process is holding just because it can reach the loopback port. Not
# added to CORSMiddleware's allow_methods (GET-only, above) on purpose: even
# a compromised/malicious page loaded in the webview can't reach this route
# via fetch() -- only Rust's direct HTTP calls (not subject to CORS) can.
_runtime_state = {
    "etherscan_api_key": os.getenv("TXGRAPH_ETHERSCAN_API_KEY", ""),
    "internal_token": os.getenv("TXGRAPH_INTERNAL_TOKEN", ""),
}


class ApiKeyUpdate(BaseModel):
    api_key: str


@app.post("/internal/api-key")
def set_internal_api_key(body: ApiKeyUpdate, request: Request):
    token = request.headers.get("x-internal-token", "")
    expected = _runtime_state["internal_token"]
    # constant-time compare -- this guards a loopback-only, write-only route,
    # but there's no reason to make timing attacks even theoretically easier
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid or missing internal token")
    _runtime_state["etherscan_api_key"] = body.api_key
    return {"status": "ok"}


@app.get("/stats/overview")
def stats_overview():
    """True totals for the dashboard's Overview stat cards. /wallets,
    /clusters, and /alerts are all capped (50/200-by-request, 100, 100
    respectively, when unfiltered) -- real row counts on a real database
    are far larger (found live: 52,581 wallets, 70,254 clusters, 197
    alerts vs. the 200/100/100 those endpoints return unfiltered), so the
    Overview page must not use len() of those capped responses as if it
    were a total. This endpoint exists so it doesn't have to."""
    if using_mock():
        mock = load_mock()
        return {
            "wallet_count": len(mock["wallets"]),
            "cluster_count": len(mock["clusters"]),
            "alert_count": len(mock["alerts"]),
        }
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM wallet")
            wallet_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM cluster")
            cluster_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM alert")
            alert_count = cur.fetchone()[0]
    return {"wallet_count": wallet_count, "cluster_count": cluster_count, "alert_count": alert_count}


@app.get("/wallets")
def list_wallets(limit: int = 50):
    if using_mock():
        return load_mock()["wallets"][:limit]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wallet_id, address, address_type, risk_score, tx_count
                   FROM wallet ORDER BY tx_count DESC NULLS LAST LIMIT %s""",
                (limit,),
            )
            return rows_as_dicts(cur)


@app.get("/wallets/{wallet_id}")
def get_wallet(wallet_id: int):
    if using_mock():
        for w in load_mock()["wallets"]:
            if w["wallet_id"] == wallet_id:
                return w
        raise HTTPException(404, "wallet not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wallet_id, address, address_type, risk_score, tx_count
                   FROM wallet WHERE wallet_id = %s""",
                (wallet_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "wallet not found")
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


CLUSTER_COLUMNS = """c.cluster_id, c.run_id, c.community_label, c.member_count, c.cluster_type,
                      c.risk_score, c.modularity_score,
                      ar.gpu_device AS analysis_backend, ar.backend_library_version AS library_version"""


@app.get("/clusters")
def list_clusters(run_id: Optional[int] = None):
    if using_mock():
        clusters = load_mock()["clusters"]
        # Mock mode has no analysis_run to join against -- additive fields
        # default to null rather than a fabricated value.
        return [{**{k: v for k, v in c.items() if k != "members"}, "analysis_backend": None, "library_version": None} for c in clusters]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if run_id is not None:
                cur.execute(
                    f"""SELECT {CLUSTER_COLUMNS}
                        FROM cluster c JOIN analysis_run ar ON ar.run_id = c.run_id
                        WHERE c.run_id = %s""",
                    (run_id,),
                )
            else:
                cur.execute(
                    f"""SELECT {CLUSTER_COLUMNS}
                        FROM cluster c JOIN analysis_run ar ON ar.run_id = c.run_id
                        ORDER BY c.run_id DESC LIMIT 100"""
                )
            return rows_as_dicts(cur)


@app.get("/clusters/{cluster_id}")
def get_cluster(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return {**{k: v for k, v in c.items() if k != "members"}, "analysis_backend": None, "library_version": None}
        raise HTTPException(404, "cluster not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT {CLUSTER_COLUMNS}
                    FROM cluster c JOIN analysis_run ar ON ar.run_id = c.run_id
                    WHERE c.cluster_id = %s""",
                (cluster_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "cluster not found")
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


@app.get("/cluster-membership")
def cluster_membership(cluster_ids: str):
    """wallet_id -> [cluster_id, ...] for the given clusters, in one round
    trip. Replaces a frontend loop that called /clusters/{id}/members once
    per cluster (100 requests on every dashboard load against real data --
    invisible against mock's single cluster, ~48s against a real DB)."""
    try:
        ids = [int(x) for x in cluster_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "cluster_ids must be a comma-separated list of integers")
    if not ids:
        return {}
    if using_mock():
        result: dict[int, list[int]] = {}
        for c in load_mock()["clusters"]:
            if c["cluster_id"] not in ids:
                continue
            for m in c.get("members", []):
                result.setdefault(m["wallet_id"], []).append(c["cluster_id"])
        return result
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wallet_id, array_agg(cluster_id ORDER BY cluster_id)
                   FROM cluster_member WHERE cluster_id = ANY(%s) GROUP BY wallet_id""",
                (ids,),
            )
            return {wallet_id: member_cluster_ids for wallet_id, member_cluster_ids in cur.fetchall()}


@app.get("/clusters/{cluster_id}/members")
def cluster_members(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return c.get("members", [])
        raise HTTPException(404, "cluster not found")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT cm.wallet_id, w.address, cm.membership_score, cm.is_core_node
                   FROM cluster_member cm JOIN wallet w ON w.wallet_id = cm.wallet_id
                   WHERE cm.cluster_id = %s""",
                (cluster_id,),
            )
            return rows_as_dicts(cur)


@app.get("/alerts")
def list_alerts(status: Optional[str] = None):
    if using_mock():
        alerts = load_mock()["alerts"]
        return [a for a in alerts if status is None or a["review_status"] == status]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    """SELECT alert_id, run_id, cluster_id, wallet_id, pattern_type, severity, confidence, review_status
                       FROM alert WHERE review_status = %s""",
                    (status,),
                )
            else:
                cur.execute(
                    """SELECT alert_id, run_id, cluster_id, wallet_id, pattern_type, severity, confidence, review_status
                       FROM alert ORDER BY detected_at DESC LIMIT 100"""
                )
            return rows_as_dicts(cur)


@app.get("/snapshots")
def list_snapshots():
    if using_mock():
        return load_mock()["snapshots"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT snapshot_id, snapshot_name, start_block, end_block, vertex_count, edge_count, is_directed
                   FROM graph_snapshot ORDER BY created_at DESC"""
            )
            return rows_as_dicts(cur)


@app.get("/snapshots/{snapshot_id}/edges")
def snapshot_edges(snapshot_id: int, limit: int = 500):
    if using_mock():
        edges = load_mock()["edges"].get(str(snapshot_id), [])
        return edges[:limit]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT src_wallet_id, dst_wallet_id, edge_weight, transfer_count
                   FROM snapshot_edge WHERE snapshot_id = %s LIMIT %s""",
                (snapshot_id, limit),
            )
            return rows_as_dicts(cur)


@app.get("/runs/{run_id}/metrics")
def run_metrics(run_id: int, metric_name: str = "pagerank", limit: int = 50):
    if using_mock():
        run = load_mock()["runs"].get(str(run_id), {})
        # Mock mode has no analysis_run to join against -- additive fields
        # default to null rather than a fabricated value.
        return [{**row, "analysis_backend": None, "library_version": None} for row in run.get(metric_name, [])[:limit]]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT vm.wallet_id, vm.metric_value, vm.rank_position,
                          ar.gpu_device AS analysis_backend, ar.backend_library_version AS library_version
                   FROM vertex_metric vm JOIN analysis_run ar ON ar.run_id = vm.run_id
                   WHERE vm.run_id = %s AND vm.metric_name = %s ORDER BY vm.rank_position LIMIT %s""",
                (run_id, metric_name, limit),
            )
            return rows_as_dicts(cur)


# ── FrameCheck (Phase 7) ────────────────────────────────────────────────────

# Mirrors graph.detectors.DETECTOR_NAMES -- duplicated rather than imported
# so this API layer keeps its "no GPU/graph-stack import needed" footprint
# (graph.detectors pulls in graph.run_analysis at module level).
FLAG_DETECTORS = ("bfs_proximity", "sybil_cluster", "pagerank_threshold", "impurity", "engaged_proximity")


def resolve_latest_run_ids(cur, snapshot_id: int, detectors=FLAG_DETECTORS) -> list:
    """Each flag detector can be re-run on the same snapshot (manual
    re-runs, re-detect after a correction, etc.), leaving older runs' flag
    rows sitting in the table alongside the newest run's. Counting all of
    them together double-counts. Returns the run_id of the most recent
    DONE run per detector on this snapshot -- resolved from analysis_run
    directly (not by joining through flag), so a detector that is latest
    but flagged nobody (e.g. engaged_proximity with 0 flags) still
    contributes its own run_id rather than being silently skipped in
    favor of an older run that happened to produce flags."""
    cur.execute(
        """
        SELECT DISTINCT ON (al.algorithm_name) ar.run_id
        FROM analysis_run ar JOIN algorithm al ON al.algorithm_id = ar.algorithm_id
        WHERE ar.snapshot_id = %s AND ar.status = 'DONE' AND al.algorithm_name = ANY(%s)
        ORDER BY al.algorithm_name, ar.run_id DESC
        """,
        (snapshot_id, list(detectors)),
    )
    return [row[0] for row in cur.fetchall()]


@app.get("/flags")
def list_flags(run_id: Optional[int] = None, detector: Optional[str] = None,
                wallet_id: Optional[int] = None, snapshot_id: Optional[int] = None, limit: int = 100):
    """snapshot_id (ignored if run_id is given explicitly) dedupes to the
    latest run per detector on that snapshot -- see resolve_latest_run_ids."""
    if using_mock():
        flags = load_mock().get("flags", [])
        filtered = [
            f for f in flags
            if (run_id is None or f["run_id"] == run_id)
            and (detector is None or f["detector"] == detector)
            and (wallet_id is None or f["wallet_id"] == wallet_id)
        ]
        return filtered[:limit]
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = "SELECT flag_id, run_id, wallet_id, detector, score, threshold, created_at FROM flag WHERE 1=1"
            params = []
            if run_id is not None:
                query += " AND run_id = %s"
                params.append(run_id)
            elif snapshot_id is not None:
                latest_run_ids = resolve_latest_run_ids(
                    cur, snapshot_id, (detector,) if detector is not None else FLAG_DETECTORS
                )
                query += " AND run_id = ANY(%s)"
                params.append(latest_run_ids)
            if detector is not None:
                query += " AND detector = %s"
                params.append(detector)
            if wallet_id is not None:
                query += " AND wallet_id = %s"
                params.append(wallet_id)
            query += " ORDER BY flag_id LIMIT %s"
            params.append(limit)
            cur.execute(query, params)
            return rows_as_dicts(cur)


@app.get("/flags/{flag_id}/evidence")
def flag_evidence_endpoint(flag_id: int):
    """Returns whatever flag_evidence rows already exist for this flag --
    does NOT compute evidence on demand (that's graph/evidence.py's job,
    run offline/by the audit engine; a GET here stays fast and read-only).
    Empty list, not an error, if evidence hasn't been collected yet."""
    if using_mock():
        return load_mock().get("flag_evidence", {}).get(str(flag_id), [])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT tx_hash, hop, block_number, role FROM flag_evidence
                   WHERE flag_id = %s ORDER BY block_number, hop""",
                (flag_id,),
            )
            return rows_as_dicts(cur)


@app.get("/flags/{flag_id}/audit")
def flag_audit_endpoint(flag_id: int):
    """The most recent flag_audit row for this flag -- 404 if it hasn't
    been audited yet (distinct from "audited, verdict robust", which is a
    normal 200)."""
    if using_mock():
        result = load_mock().get("flag_audit", {}).get(str(flag_id))
        if result is None:
            raise HTTPException(404, "flag not audited yet")
        return result
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT flag_audit_id, flag_id, verdict, framing_cost_wei, framing_gas,
                          minimal_tx_set, method, backend, runtime_ms, created_at
                   FROM flag_audit WHERE flag_id = %s ORDER BY created_at DESC LIMIT 1""",
                (flag_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "flag not audited yet")
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


@app.get("/audit/summary")
def audit_summary(run_id: Optional[int] = None, snapshot_id: Optional[int] = None):
    """Per detector: how many flags have been audited, what share came
    back frameable, and the framing-cost min/median/max among the
    frameable ones (robust flags have no framing cost to include).

    snapshot_id (ignored if run_id is given explicitly) dedupes to the
    latest run per detector on that snapshot, so re-running a detector
    on the same snapshot doesn't double-count flags audited under an
    older run alongside the newest one -- see resolve_latest_run_ids."""
    if using_mock():
        return load_mock().get("audit_summary", [])
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = """
                SELECT f.detector,
                       COUNT(*) AS total_audited,
                       COUNT(*) FILTER (WHERE fa.verdict = 'frameable') AS frameable_count,
                       MIN(fa.framing_cost_wei) FILTER (WHERE fa.verdict = 'frameable') AS framing_cost_min_wei,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fa.framing_cost_wei)
                           FILTER (WHERE fa.verdict = 'frameable') AS framing_cost_median_wei,
                       MAX(fa.framing_cost_wei) FILTER (WHERE fa.verdict = 'frameable') AS framing_cost_max_wei
                FROM flag_audit fa JOIN flag f ON f.flag_id = fa.flag_id
            """
            params = []
            if run_id is not None:
                query += " WHERE f.run_id = %s"
                params.append(run_id)
            elif snapshot_id is not None:
                latest_run_ids = resolve_latest_run_ids(cur, snapshot_id)
                query += " WHERE f.run_id = ANY(%s)"
                params.append(latest_run_ids)
            query += " GROUP BY f.detector ORDER BY f.detector"
            cur.execute(query, params)
            rows = rows_as_dicts(cur)
            for row in rows:
                row["frameable_share"] = row["frameable_count"] / row["total_audited"] if row["total_audited"] else 0.0
            return rows
