"""
The API layer both halves of the project share (project bible, Section 7.2).

Two modes, controlled by the FRAMECHECK_API_MODE env var:
  - "mock" (default): serves api/mock_data.json. Your partner runs this from
    day one -- no Postgres, no GPU, no ingestion needed at all.
  - "live": queries the real Postgres schema.

The endpoint shapes are IDENTICAL in both modes -- that's the whole point.
Switching FRAMECHECK_API_MODE=live is meant to be a data-source swap for the
frontend, not a rebuild (see the bible's Section 7.3).

Run it:
    uvicorn api.main:app --reload --port 8000
    curl localhost:8000/wallets
    curl localhost:8000/docs      # interactive OpenAPI docs, free from FastAPI
"""
import csv
import heapq
import io
import json
import logging
import os
import pathlib
import re
import secrets
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Literal, Optional

import psycopg2
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg2 import InterfaceError, OperationalError
from psycopg2 import pool as pg_pool
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    String,
    Text,
    and_,
    any_,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    true,
    type_coerce,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, aggregate_order_by, distinct_on
from sqlalchemy.orm import aliased
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from api import auth, dbsetup, jobs, pagination, rbac
from db.orm.engine import reset_engine
from db.orm.models import (
    Alert,
    Algorithm,
    AnalysisRun,
    AppUser,
    Cluster,
    ClusterMember,
    Flag,
    FlagAudit,
    FlagEvidence,
    FlagLatestAudit,
    GraphSnapshot,
    Label,
    SnapshotEdge,
    Transaction,
    VertexMetric,
    Wallet,
    WalletLabel,
)
from db.orm.session import execute_on_cursor, get_session
from ingestion import config
from ingestion.etherscan_client import EtherscanClient

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

# What "the database is unreachable" looks like from either data path: the
# raw psycopg2 pool raises psycopg2's classes, while SQLAlchemy wraps the
# same driver errors in its own, which are NOT subclasses of psycopg2's --
# without the second pair, a dead database behind an ORM endpoint would
# surface as a 500 instead of a 503.
DB_UNAVAILABLE = (OperationalError, InterfaceError, sa_exc.OperationalError, sa_exc.InterfaceError)


def close_pool() -> None:
    """Drops the pool so the next request builds a fresh one. Called on
    shutdown and whenever the connection settings change -- a pooled
    connection to the old database would otherwise outlive the setting that
    created it."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            log.info("Postgres connection pool closed")
            _pool = None
    # The ORM's engine pools connections too, against the same settings.
    reset_engine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_pool()


app = FastAPI(
    title="FrameCheck API",
    version="0.1.0",
    description="Shared contract between the GPU/backend half and the frontend half. See docs/API_CONTRACT.md.",
    lifespan=lifespan,
)


# Every route that writes to the database or changes a setting -- verified
# against the actual route table (grep '@app\.(post|put|delete|patch)' finds
# nothing outside these four prefixes and /auth/*, and /auth/* is exempt on
# purpose: public-demo mode disables sign-in entirely, so there is no session
# for those routes to create or destroy, and they touch no FrameCheck data).
# A bare "/jobs" POST creates a job; "/jobs/{id}/cancel" needs the prefix
# form, hence checking both the exact path and the "/"-suffixed prefix.
PUBLIC_DEMO_BLOCKED_PREFIXES = ("/db/", "/jobs/", "/ingest/", "/internal/")


def _is_mutating_path(path: str) -> bool:
    return path == "/jobs" or path.startswith(PUBLIC_DEMO_BLOCKED_PREFIXES)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Gates every route except auth.PUBLIC_PATHS. A middleware rather
    than a per-route dependency so that a route added later is protected
    by default -- forgetting to add a dependency would silently expose a
    new endpoint, while forgetting to add a path to PUBLIC_PATHS only
    makes a public route ask for a session, which is the safe direction
    to fail in."""
    if auth.public_demo_mode() and request.method == "POST" and _is_mutating_path(request.url.path):
        return JSONResponse(status_code=403, content={
            "detail": "read-only public demo: this action is disabled on the shared link"})
    if request.method == "OPTIONS":
        return await _call(request, call_next)
    # The minimum role for this route (api/rbac.py's central table).
    needed = rbac.required_role(app, request.scope)
    if needed == rbac.PUBLIC or request.url.path in auth.PUBLIC_PATHS:
        return await _call(request, call_next)
    # Public demo FIRST: a shared demo link usually also runs with sign-in
    # disabled, and an anonymous visitor on it must be a viewer, never the
    # local admin below.
    if auth.public_demo_mode():
        request.state.user = rbac.DEMO_VIEWER
        if not rbac.allows(rbac.VIEWER, needed):
            return _forbidden(rbac.VIEWER, needed)
        return await _call(request, call_next)
    if auth.auth_mode() == "disabled":
        # Desktop sidecar / local development: a local admin, exactly as
        # before roles existed.
        request.state.user = rbac.LOCAL_ADMIN
        return await _call(request, call_next)

    token = request.cookies.get(auth.SESSION_COOKIE)
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        return JSONResponse(status_code=401, content={"detail": "sign-in required"})
    try:
        request.state.session = auth.read_session(token)
    except auth.AuthError as e:
        # The reason (expired vs tampered vs unconfigured secret) goes to
        # the log, not to the caller.
        log.info("Rejected session on %s: %s", request.url.path, e)
        return JSONResponse(status_code=401, content={"detail": "sign-in required"})

    # The role comes from the database on every request, never from the
    # token, so a role change or deactivation applies immediately. Mock mode
    # has no users table: a signed-in caller there is a viewer, which is
    # every read (and mock mode refuses every write anyway).
    if using_mock():
        user = {**rbac.DEMO_VIEWER, "user_id": request.state.session["user_id"]}
    else:
        try:
            user = await run_in_threadpool(rbac.load_user, request.state.session["user_id"])
        except DB_UNAVAILABLE:
            log.exception("Database unavailable checking the session on %s", request.url.path)
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        if user is None or not user["is_active"]:
            return JSONResponse(status_code=401, content={"detail": "sign-in required"})
    request.state.user = user
    if not rbac.allows(user["role"], needed):
        return _forbidden(user["role"], needed)
    return await _call(request, call_next)


def _forbidden(role: str, needed: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={
        "detail": f"forbidden: this needs the {needed} role (you are {role})",
        "role": role, "required_role": needed})


async def _call(request: Request, call_next):
    """Runs the route, turning a dead database into a 503 *here* rather than
    in the app-level exception handler. The handler runs outside the CORS
    middleware, so its response carries no Access-Control-Allow-Origin and
    the browser reports a CORS failure instead of the real error -- which
    sent me looking for a CORS bug when Postgres had simply gone away."""
    try:
        return await call_next(request)
    except DB_UNAVAILABLE:
        log.exception("Database unavailable handling %s %s", request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})


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
    if isinstance(exc, DB_UNAVAILABLE):
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# Phase 5 ended the wildcard: a session cookie is credentialed, and the CORS
# spec refuses `Access-Control-Allow-Origin: *` together with credentials --
# the browser drops the response outright. So the origin is echoed back, but
# only for local ones: the dashboard on any localhost dev port, and the Tauri
# webview (which serves the app from http://tauri.localhost on Windows and
# tauri://localhost elsewhere). POST joins GET because signing in and out
# are mutating.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|tauri://localhost|https?://tauri\.localhost)$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
    expose_headers=pagination.EXPOSED_HEADERS,
)

MOCK_PATH = pathlib.Path(__file__).parent / "mock_data.json"


# Set by POST /db/mode so a user who has just created and populated a
# database from the Database view can switch to live data without quitting
# the app (an installed app has no .env to edit and no shell to re-launch
# from). None means "whatever the environment says", which is what every
# terminal and CI run keeps seeing.
_mode_override: str | None = None


def using_mock() -> bool:
    mode = _mode_override or os.getenv("FRAMECHECK_API_MODE", "mock")
    return mode.lower() != "live"


def load_mock() -> dict:
    return json.loads(MOCK_PATH.read_text())


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check: another thread may have won the race while we waited for the lock
                dsn = dbsetup.current_dsn()
                # Every pooled connection pays the host-resolution cost, so
                # the pool uses the same IPv4-first ordering as
                # dbsetup.connect() -- worth ~2 s per connection here, and a
                # pool opens one immediately (minconn=1). See connect().
                last_error = None
                for host in dbsetup.connect_order(dsn.get("host")):
                    try:
                        _pool = pg_pool.ThreadedConnectionPool(
                            minconn=1, maxconn=10, **{**dsn, "host": host})
                        break
                    except OperationalError as e:
                        last_error = e
                else:
                    raise last_error
                log.info("Postgres connection pool created (min=1, max=10)")
    return _pool


# RAW SQL (allowed): the psycopg2 pool kept for the raw-SQL endpoints tagged below; the ORM path is db/orm (docs/API_CONTRACT.md, "ORM coverage and permitted raw SQL")
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


def orm_rows(session, statement) -> list:
    """An ORM select's rows as dicts, keyed by the selected column names (or
    their .label()s) in select order -- the same shape rows_as_dicts gives
    the raw-SQL endpoints, so a response does not change when its endpoint
    moves onto the ORM."""
    return [dict(row._mapping) for row in session.execute(statement)]


def orm_row(session, statement) -> dict | None:
    row = session.execute(statement).first()
    return dict(row._mapping) if row is not None else None


@app.get("/health")
def health():
    return {"status": "ok", "mode": "mock" if using_mock() else "live",
            "public_demo": auth.public_demo_mode()}


# ── Etherscan key handoff (desktop-app packaging, Part 2) ──────────────────
# The Rust shell owns the key in the OS keychain and never sends it to the
# webview -- it hands the key to *this* process once, as an env var, at
# spawn time. Held in memory only from here on: never written to disk, never
# logged, never echoed back. This is deliberately separate from
# ingestion/config.py's ETHERSCAN_API_KEY (.env-sourced) -- that constant
# stays exactly as-is for direct CLI/dev use (`python -m ingestion...`,
# scripts/run_pipeline.py); the two are unrelated and never synced.
#
# FRAMECHECK_INTERNAL_TOKEN is a random value Rust generates fresh per app
# launch and passes at spawn alongside the key -- /internal/api-key requires
# it on every call, so no other local process can silently overwrite the key
# this process is holding just because it can reach the loopback port. Not
# passed to any page: only Rust knows the token, so a compromised or malicious
# page loaded in the webview cannot call this route even though POST is an
# allowed CORS method.
_runtime_state = {
    "etherscan_api_key": os.getenv("FRAMECHECK_ETHERSCAN_API_KEY", ""),
    "internal_token": os.getenv("FRAMECHECK_INTERNAL_TOKEN", ""),
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


def _tier_counts_from_scores(scores: list) -> dict:
    """Mirrors frontend/dashboard.html's riskTier() bucketing exactly
    (high >= 67, med 34-66.99, low < 34, unknown = null/missing) -- used
    by mock mode, which has no SQL to delegate this to."""
    counts = {"low": 0, "med": 0, "high": 0, "unknown": 0}
    for score in scores:
        if score is None:
            counts["unknown"] += 1
        elif score >= 67:
            counts["high"] += 1
        elif score >= 34:
            counts["med"] += 1
        else:
            counts["low"] += 1
    return counts


@app.get("/stats/overview")
def stats_overview():
    """True totals for the dashboard's Overview page. /wallets,
    /clusters, and /alerts are all capped (50/200-by-request, 100, 100
    respectively, when unfiltered) -- real row counts and breakdowns on a
    real database are far larger (found live: 52,581 wallets, 70,254
    clusters, 197 alerts -- all 197 marked NEW, 100 HIGH severity -- vs.
    the 200/100/100-row pages those endpoints return unfiltered), so the
    Overview page must not compute totals or breakdowns by filtering
    those capped responses as if they were the whole dataset. This
    endpoint exists so it doesn't have to, for every Overview number:
    the three raw counts, the alert new-count and severity breakdown,
    and the wallet risk-tier breakdown."""
    if using_mock():
        mock = load_mock()
        alerts = mock["alerts"]
        severity_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for a in alerts:
            if a["severity"] in severity_counts:
                severity_counts[a["severity"]] += 1
        return {
            "wallet_count": len(mock["wallets"]),
            "cluster_count": len(mock["clusters"]),
            "alert_count": len(alerts),
            "alert_new_count": sum(1 for a in alerts if a["review_status"] == "NEW"),
            "alert_severity_counts": severity_counts,
            "wallet_risk_tier_counts": _tier_counts_from_scores([w["risk_score"] for w in mock["wallets"]]),
        }
    with get_session() as db:
        wallet_count = db.scalar(select(func.count()).select_from(Wallet))
        cluster_count = db.scalar(select(func.count()).select_from(Cluster))
        alert_count = db.scalar(select(func.count()).select_from(Alert))
        alert_new_count = db.scalar(select(func.count()).select_from(Alert).where(Alert.review_status == "NEW"))
        severity_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for severity, count in db.execute(select(Alert.severity, func.count()).group_by(Alert.severity)):
            if severity in severity_counts:
                severity_counts[severity] = count
        # Same buckets as the dashboard's riskTier(); one scan, four
        # FILTERed counts.
        tiers = db.execute(select(
            func.count().filter(Wallet.risk_score.is_(None)).label("unknown"),
            func.count().filter(Wallet.risk_score >= 67).label("high"),
            func.count().filter(and_(Wallet.risk_score >= 34, Wallet.risk_score < 67)).label("med"),
            func.count().filter(Wallet.risk_score < 34).label("low"),
        )).one()
    return {
        "wallet_count": wallet_count,
        "cluster_count": cluster_count,
        "alert_count": alert_count,
        "alert_new_count": alert_new_count,
        "alert_severity_counts": severity_counts,
        "wallet_risk_tier_counts": {"low": tiers.low, "med": tiers.med, "high": tiers.high, "unknown": tiers.unknown},
    }


WALLET_COLUMNS = (Wallet.wallet_id, Wallet.address, Wallet.address_type, Wallet.risk_score, Wallet.tx_count)


@app.get("/wallets")
def list_wallets(limit: int = 50):
    if using_mock():
        return load_mock()["wallets"][:limit]
    with get_session() as db:
        # NULLS LAST matches idx_wallet_tx_count (migration 007), so the
        # planner walks the index instead of sorting every wallet.
        return orm_rows(db, select(*WALLET_COLUMNS)
                        .order_by(Wallet.tx_count.desc().nulls_last())
                        .limit(limit))


@app.get("/wallets/{wallet_id}")
def get_wallet(wallet_id: int):
    if using_mock():
        for w in load_mock()["wallets"]:
            if w["wallet_id"] == wallet_id:
                return w
        raise HTTPException(404, "wallet not found")
    with get_session() as db:
        wallet = db.get(Wallet, wallet_id)
        if wallet is None:
            raise HTTPException(404, "wallet not found")
        return {col.key: getattr(wallet, col.key) for col in WALLET_COLUMNS}


# The analysis run's backend travels with each cluster, so the UI can say
# whether a community came from cuGraph or NetworkX.
CLUSTER_COLUMNS = (
    Cluster.cluster_id, Cluster.run_id, Cluster.community_label, Cluster.member_count,
    Cluster.cluster_type, Cluster.risk_score, Cluster.modularity_score,
    AnalysisRun.gpu_device.label("analysis_backend"),
    AnalysisRun.backend_library_version.label("library_version"),
)


def clusters_with_run():
    return select(*CLUSTER_COLUMNS).select_from(Cluster).join(Cluster.run)


@app.get("/clusters")
def list_clusters(run_id: Optional[int] = None):
    if using_mock():
        clusters = load_mock()["clusters"]
        # Mock mode has no analysis_run to join against -- additive fields
        # default to null rather than a fabricated value.
        return [{**{k: v for k, v in c.items() if k != "members"}, "analysis_backend": None, "library_version": None} for c in clusters]
    with get_session() as db:
        if run_id is not None:
            return orm_rows(db, clusters_with_run().where(Cluster.run_id == run_id))
        return orm_rows(db, clusters_with_run().order_by(Cluster.run_id.desc()).limit(100))


CLUSTER_ALGORITHM = "louvain"


# RAW SQL (allowed): part of /clusters/top-flagged's performance-critical path (below)
def resolve_cluster_run_id(cur, snapshot_id: int):
    """The newest Louvain run on this snapshot. Cluster rows and flag rows
    live under DIFFERENT analysis_run ids -- one is a Louvain run, the
    other a detector run -- so the only thing that relates them is the
    snapshot they were both computed on. Matching run_id to run_id across
    the two would silently return nothing."""
    cur.execute(
        """
        SELECT ar.run_id FROM analysis_run ar
        JOIN algorithm al ON al.algorithm_id = ar.algorithm_id
        WHERE ar.snapshot_id = %s AND al.algorithm_name = %s
        ORDER BY ar.run_id DESC LIMIT 1
        """,
        (snapshot_id, CLUSTER_ALGORITHM),
    )
    row = cur.fetchone()
    return row[0] if row else None


# RAW SQL (allowed): part of /clusters/top-flagged's performance-critical path (below)
def resolve_snapshot_for_run(cur, run_id: int):
    cur.execute("SELECT snapshot_id FROM analysis_run WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    return row[0] if row else None


# Registered BEFORE /clusters/{cluster_id}: FastAPI matches in declaration
# order, so the typed-int route would otherwise capture "top-flagged" and
# reject it as a 422 before this handler is ever reached.
# RAW SQL (allowed): performance-critical -- aggregates the mv_cluster_flag_stats materialized view (migration 008: 471 ms -> 2 ms); also pinned by tests/test_multiview_api.py
@app.get("/clusters/top-flagged")
def clusters_top_flagged(run_id: Optional[int] = None, snapshot_id: Optional[int] = None, limit: int = 8):
    """Clusters holding the most distinct flagged wallets, for Multiview's
    "Top 8 flagged". Ranked by flagged wallets rather than raw flag count
    so one wallet carrying five detector flags doesn't outrank five
    separately flagged wallets."""
    if limit < 1 or limit > 8:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 8")
    if using_mock():
        return load_mock().get("clusters_top_flagged", [])
    with get_conn() as conn:
        with conn.cursor() as cur:
            if snapshot_id is None:
                if run_id is not None:
                    snapshot_id = resolve_snapshot_for_run(cur, run_id)
                    if snapshot_id is None:
                        raise HTTPException(status_code=404, detail="no such run")
                else:
                    cur.execute("SELECT snapshot_id FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id "
                                "ORDER BY f.flag_id DESC LIMIT 1")
                    row = cur.fetchone()
                    if row is None:
                        return []
                    snapshot_id = row[0]
            cluster_run_id = resolve_cluster_run_id(cur, snapshot_id)
            if cluster_run_id is None:
                return []
            detector_run_ids = resolve_latest_run_ids(cur, snapshot_id)
            if not detector_run_ids:
                return []
            # Rank from the precomputed view (migration 008), which has
            # already done the cluster_member x flag join once. Verdicts are
            # NOT in that view -- they change on every single audit -- so
            # they are joined live below, but only for the handful of
            # clusters that survive the ranking rather than all 128,637
            # flags.
            cur.execute(
                """
                SELECT cluster_id, community_label, member_count,
                       count(DISTINCT wallet_id)   AS flagged_wallets,
                       sum(flags_for_wallet)::bigint AS flag_count
                FROM mv_cluster_flag_stats
                WHERE cluster_run_id = %s AND flag_run_id = ANY(%s)
                GROUP BY cluster_id, community_label, member_count
                ORDER BY flagged_wallets DESC, flag_count DESC, cluster_id
                LIMIT %s
                """,
                (cluster_run_id, detector_run_ids, limit),
            )
            rows = rows_as_dicts(cur)
            if not rows:
                return []

            cur.execute(
                """
                SELECT cm.cluster_id,
                       count(*) FILTER (WHERE fa.verdict = 'frameable') AS frameable_count,
                       count(fa.verdict)                                AS audited_count
                FROM cluster_member cm
                JOIN flag f ON f.wallet_id = cm.wallet_id AND f.run_id = ANY(%s)
                LEFT JOIN LATERAL (
                    SELECT verdict FROM flag_audit
                    WHERE flag_id = f.flag_id
                    ORDER BY created_at DESC, flag_audit_id DESC LIMIT 1
                ) fa ON TRUE
                WHERE cm.cluster_id = ANY(%s)
                GROUP BY cm.cluster_id
                """,
                (detector_run_ids, [r["cluster_id"] for r in rows]),
            )
            verdicts = {r["cluster_id"]: r for r in rows_as_dicts(cur)}
            for row in rows:
                verdict = verdicts.get(row["cluster_id"], {})
                row["snapshot_id"] = snapshot_id
                row["cluster_type"] = None
                row["risk_score"] = None
                row["frameable_count"] = verdict.get("frameable_count", 0)
                row["audited_count"] = verdict.get("audited_count", 0)
                # null, not 0.0: "nothing in this cluster has been audited"
                # and "none of it is frameable" are different facts.
                row["frameable_share"] = (
                    row["frameable_count"] / row["audited_count"] if row["audited_count"] else None
                )
            return rows


def select_connected_subgraph(ranked_wallet_ids: list, adjacency: dict, limit: int) -> list:
    """Pick `limit` wallets that actually hang together, not just the
    `limit` highest-ranked ones.

    Taking the top N by PageRank out of a big cluster looks reasonable and
    renders as a dust cloud: on cluster 351299 (26,594 members) the top 300
    came back as 76 disconnected fragments with 22% of nodes having no edge
    to any other node shown. That is not a picture of a cluster.

    So: start from the highest-ranked wallet and expand outward along real
    edges, taking the best-ranked frontier node each step, which yields a
    connected component. If that component is exhausted before the budget
    is, restart from the next-highest unvisited wallet -- so a panel shows
    whole structures, largest first, rather than scattered singletons.
    """
    rank = {wallet_id: i for i, wallet_id in enumerate(ranked_wallet_ids)}
    chosen, seen = [], set()
    for seed in ranked_wallet_ids:
        if len(chosen) >= limit:
            break
        if seed in seen:
            continue
        # A heap, not a sorted list: the frontier of a hub node can be
        # thousands wide, and re-sorting it on every step is what turns a
        # 300-node panel into a visible stall.
        frontier = [(rank[seed], seed)]
        seen.add(seed)
        while frontier and len(chosen) < limit:
            _, current = heapq.heappop(frontier)
            chosen.append(current)
            for neighbour in adjacency.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    heapq.heappush(frontier, (rank.get(neighbour, len(rank)), neighbour))
    return chosen


# RAW SQL (allowed): performance-critical -- hand-tuned against EXPLAIN (ANALYZE): the PageRank run is resolved
# once instead of per member (489 ms of 545 ms), and addresses/verdicts are fetched only for the nodes kept
# (1,038 ms before) -- see the comments inside
@app.get("/clusters/{cluster_id}/subgraph")
def cluster_subgraph(cluster_id: int, limit: int = 300):
    """One cluster's members and the edges between them, for a Multiview
    panel. Truncation is by connected expansion from the highest-PageRank
    member (see select_connected_subgraph), and is always reported, since a
    silently truncated graph is a misleading one."""
    if limit < 1 or limit > 2000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 2000")
    if using_mock():
        return load_mock().get("cluster_subgraphs", {}).get(str(cluster_id),
                                                            {"nodes": [], "edges": [], "truncated": False})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.cluster_id, c.run_id, c.member_count, c.community_label, ar.snapshot_id
                   FROM cluster c JOIN analysis_run ar ON ar.run_id = c.run_id
                   WHERE c.cluster_id = %s""",
                (cluster_id,),
            )
            meta = cur.fetchone()
            if meta is None:
                raise HTTPException(status_code=404, detail="no such cluster")
            _cid, _run_id, member_count, community_label, snapshot_id = meta

            # Two passes on purpose. The ranking pass needs every member
            # (connected expansion walks the whole cluster), but it only
            # needs wallet_id and PageRank. Fetching addresses and per-wallet
            # verdicts for all of them was 1,038 ms on the 26,594-member
            # cluster -- 26,594 LATERAL verdict lookups to display 150 nodes,
            # plus a Seq Scan over all 610,055 wallets to get addresses.
            # The detail pass below runs only for the nodes actually kept.
            #
            # PageRank lives in vertex_metric under its own run; absent (not
            # yet computed) it is NULL and ordering falls back to wallet_id,
            # which is stable rather than arbitrary.
            # Resolve the PageRank run ONCE. Done per wallet inside a
            # LATERAL, the planner walked all 13 analysis_run rows for each
            # of 26,594 members -- 345,722 index probes for 26,594 answers,
            # 489 ms of the endpoint's 545 ms. With the run fixed, the
            # existing (run_id, wallet_id, metric_name) unique index answers
            # each member in one probe.
            cur.execute(
                """
                SELECT ar.run_id FROM analysis_run ar
                WHERE ar.snapshot_id = %s
                  AND EXISTS (SELECT 1 FROM vertex_metric vm
                              WHERE vm.run_id = ar.run_id AND vm.metric_name = 'pagerank')
                ORDER BY ar.run_id DESC LIMIT 1
                """,
                (snapshot_id,),
            )
            pagerank_run = cur.fetchone()
            pagerank_run_id = pagerank_run[0] if pagerank_run else None

            cur.execute(
                """
                SELECT cm.wallet_id, vm.metric_value AS pagerank
                FROM cluster_member cm
                LEFT JOIN vertex_metric vm
                       ON vm.run_id = %s AND vm.wallet_id = cm.wallet_id
                      AND vm.metric_name = 'pagerank'
                WHERE cm.cluster_id = %s
                ORDER BY vm.metric_value DESC NULLS LAST, cm.wallet_id
                """,
                (pagerank_run_id, cluster_id),
            )
            ranked = rows_as_dicts(cur)

            # Every edge inside the cluster, so the expansion below walks
            # real structure. Cheap even at the extreme: the 26,594-member
            # cluster's 28,870 internal edges come back in ~0.05s.
            cur.execute(
                """SELECT src_wallet_id, dst_wallet_id, edge_weight, transfer_count
                   FROM snapshot_edge
                   WHERE snapshot_id = %s
                     AND src_wallet_id IN (SELECT wallet_id FROM cluster_member WHERE cluster_id = %s)
                     AND dst_wallet_id IN (SELECT wallet_id FROM cluster_member WHERE cluster_id = %s)""",
                (snapshot_id, cluster_id, cluster_id),
            )
            internal_edges = rows_as_dicts(cur)

            adjacency = {}
            for edge in internal_edges:
                adjacency.setdefault(edge["src_wallet_id"], set()).add(edge["dst_wallet_id"])
                adjacency.setdefault(edge["dst_wallet_id"], set()).add(edge["src_wallet_id"])

            keep = select_connected_subgraph([n["wallet_id"] for n in ranked], adjacency, limit)
            keep_set = set(keep)
            pagerank_by_wallet = {n["wallet_id"]: n["pagerank"] for n in ranked}

            # Detail pass: addresses and verdicts for the kept nodes only.
            cur.execute(
                """
                SELECT w.wallet_id, w.address,
                       COALESCE(fl.flag_count, 0) AS flag_count,
                       CASE WHEN COALESCE(fl.flag_count, 0) = 0 THEN 'unflagged'
                            WHEN fl.audited = 0 THEN 'unaudited'
                            WHEN fl.any_frameable = 1 THEN 'frameable'
                            ELSE 'robust' END AS verdict
                FROM wallet w
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS flag_count,
                           MAX(CASE WHEN a.verdict = 'frameable' THEN 1 ELSE 0 END) AS any_frameable,
                           COUNT(a.verdict) AS audited
                    FROM flag f
                    JOIN analysis_run far ON far.run_id = f.run_id
                    LEFT JOIN LATERAL (
                        SELECT verdict FROM flag_audit
                        WHERE flag_id = f.flag_id
                        ORDER BY created_at DESC, flag_audit_id DESC LIMIT 1
                    ) a ON TRUE
                    WHERE f.wallet_id = w.wallet_id AND far.snapshot_id = %s
                ) fl ON TRUE
                WHERE w.wallet_id = ANY(%s)
                """,
                (snapshot_id, keep),
            )
            detail = {row["wallet_id"]: row for row in rows_as_dicts(cur)}

            # Ordered by the ranking pass, so the response still reads
            # highest-PageRank first.
            nodes = []
            for wallet_id in (n["wallet_id"] for n in ranked if n["wallet_id"] in keep_set):
                row = detail.get(wallet_id, {"wallet_id": wallet_id, "address": None,
                                             "flag_count": 0, "verdict": "unflagged"})
                nodes.append({**row, "pagerank": pagerank_by_wallet.get(wallet_id)})
            edges = [e for e in internal_edges
                     if e["src_wallet_id"] in keep_set and e["dst_wallet_id"] in keep_set]
            return {
                "cluster_id": cluster_id,
                "community_label": community_label,
                "snapshot_id": snapshot_id,
                "member_count": member_count,
                "nodes": nodes,
                "edges": edges,
                "truncated": member_count is not None and len(nodes) < member_count,
            }


@app.get("/clusters/{cluster_id}")
def get_cluster(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return {**{k: v for k, v in c.items() if k != "members"}, "analysis_backend": None, "library_version": None}
        raise HTTPException(404, "cluster not found")
    with get_session() as db:
        row = orm_row(db, clusters_with_run().where(Cluster.cluster_id == cluster_id))
        if row is None:
            raise HTTPException(404, "cluster not found")
        return row


@app.get("/cluster-membership")
def cluster_membership(cluster_ids: str):
    """wallet_id -> [cluster_id, ...] for the given clusters, in one round
    trip. Replaces a frontend loop that called /clusters/{id}/members once
    per cluster (100 requests on every dashboard load against real data --
    invisible against mock's single cluster, ~48s against a real DB)."""
    try:
        ids = [int(x) for x in cluster_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "cluster_ids must be a comma-separated list of integers") from None
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
    with get_session() as db:
        rows = db.execute(
            select(ClusterMember.wallet_id,
                   func.array_agg(aggregate_order_by(ClusterMember.cluster_id, ClusterMember.cluster_id)))
            .where(any_of(ClusterMember.cluster_id, ids, BigInteger))
            .group_by(ClusterMember.wallet_id))
        return {wallet_id: member_cluster_ids for wallet_id, member_cluster_ids in rows}


@app.get("/clusters/{cluster_id}/members")
def cluster_members(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return c.get("members", [])
        raise HTTPException(404, "cluster not found")
    with get_session() as db:
        return orm_rows(db, select(ClusterMember.wallet_id, Wallet.address,
                                   ClusterMember.membership_score, ClusterMember.is_core_node)
                        .select_from(ClusterMember).join(ClusterMember.wallet)
                        .where(ClusterMember.cluster_id == cluster_id))


ALERT_COLUMNS = (Alert.alert_id, Alert.run_id, Alert.cluster_id, Alert.wallet_id, Alert.pattern_type,
                 Alert.severity, Alert.confidence, Alert.review_status)


@app.get("/alerts")
def list_alerts(status: Optional[str] = None):
    if using_mock():
        alerts = load_mock()["alerts"]
        return [a for a in alerts if status is None or a["review_status"] == status]
    with get_session() as db:
        if status:
            return orm_rows(db, select(*ALERT_COLUMNS).where(Alert.review_status == status))
        return orm_rows(db, select(*ALERT_COLUMNS).order_by(Alert.detected_at.desc()).limit(100))


@app.get("/snapshots")
def list_snapshots():
    if using_mock():
        return load_mock()["snapshots"]
    with get_session() as db:
        return orm_rows(db, select(GraphSnapshot.snapshot_id, GraphSnapshot.snapshot_name,
                                   GraphSnapshot.start_block, GraphSnapshot.end_block,
                                   GraphSnapshot.vertex_count, GraphSnapshot.edge_count,
                                   GraphSnapshot.is_directed)
                        .order_by(GraphSnapshot.created_at.desc()))


@app.get("/snapshots/{snapshot_id}/edges")
def snapshot_edges(snapshot_id: int, limit: int = 500):
    if using_mock():
        edges = load_mock()["edges"].get(str(snapshot_id), [])
        return edges[:limit]
    with get_session() as db:
        return orm_rows(db, select(SnapshotEdge.src_wallet_id, SnapshotEdge.dst_wallet_id,
                                   SnapshotEdge.edge_weight, SnapshotEdge.transfer_count)
                        .where(SnapshotEdge.snapshot_id == snapshot_id)
                        .limit(limit))


@app.get("/runs/{run_id}/metrics")
def run_metrics(run_id: int, metric_name: str = "pagerank", limit: int = 50):
    if using_mock():
        run = load_mock()["runs"].get(str(run_id), {})
        # Mock mode has no analysis_run to join against -- additive fields
        # default to null rather than a fabricated value.
        return [{**row, "analysis_backend": None, "library_version": None} for row in run.get(metric_name, [])[:limit]]
    with get_session() as db:
        return orm_rows(db, select(VertexMetric.wallet_id, VertexMetric.metric_value, VertexMetric.rank_position,
                                   AnalysisRun.gpu_device.label("analysis_backend"),
                                   AnalysisRun.backend_library_version.label("library_version"))
                        .select_from(VertexMetric).join(VertexMetric.run)
                        .where(VertexMetric.run_id == run_id, VertexMetric.metric_name == metric_name)
                        .order_by(VertexMetric.rank_position)
                        .limit(limit))


# ── FrameCheck (Phase 7) ────────────────────────────────────────────────────

# Mirrors graph.detectors.DETECTOR_NAMES -- duplicated rather than imported
# so this API layer keeps its "no GPU/graph-stack import needed" footprint
# (graph.detectors pulls in graph.run_analysis at module level).
FLAG_DETECTORS = ("bfs_proximity", "sybil_cluster", "pagerank_threshold", "impurity", "engaged_proximity")


def latest_run_ids_statement(snapshot_id: int, detectors=FLAG_DETECTORS):
    """Each flag detector can be re-run on the same snapshot (manual
    re-runs, re-detect after a correction, etc.), leaving older runs' flag
    rows sitting in the table alongside the newest run's. Counting all of
    them together double-counts. This selects the run_id of the most recent
    DONE run per detector on this snapshot -- resolved from analysis_run
    directly (not by joining through flag), so a detector that is latest
    but flagged nobody (e.g. engaged_proximity with 0 flags) still
    contributes its own run_id rather than being silently skipped in
    favor of an older run that happened to produce flags."""
    return (select(AnalysisRun.run_id)
            .join(AnalysisRun.algorithm)
            .where(AnalysisRun.snapshot_id == snapshot_id,
                   AnalysisRun.status == "DONE",
                   Algorithm.algorithm_name == any_(literal(list(detectors), ARRAY(String))))
            .ext(distinct_on(Algorithm.algorithm_name))
            .order_by(Algorithm.algorithm_name, AnalysisRun.run_id.desc()))


def latest_run_ids(session, snapshot_id: int, detectors=FLAG_DETECTORS) -> list:
    """latest_run_ids_statement() through a session."""
    return list(session.scalars(latest_run_ids_statement(snapshot_id, detectors)))


def resolve_latest_run_ids(cur, snapshot_id: int, detectors=FLAG_DETECTORS) -> list:
    """The same statement on an open psycopg2 cursor, for
    /clusters/top-flagged, whose materialized-view query stays raw."""
    return [row[0] for row in execute_on_cursor(cur, latest_run_ids_statement(snapshot_id, detectors))]


def any_of(column, values: list, element_type):
    """`column = ANY(:values)` with the list bound as ONE array parameter, so
    the statement text is the same however many values there are."""
    return column == any_(literal(list(values), ARRAY(element_type)))


def in_run_ids(column, run_ids: list):
    return any_of(column, run_ids, BigInteger)


FLAG_COLUMNS = (Flag.flag_id, Flag.run_id, Flag.wallet_id, Flag.detector, Flag.score, Flag.threshold, Flag.created_at)


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
    with get_session() as db:
        query = select(*FLAG_COLUMNS)
        if run_id is not None:
            query = query.where(Flag.run_id == run_id)
        elif snapshot_id is not None:
            detectors = (detector,) if detector is not None else FLAG_DETECTORS
            query = query.where(in_run_ids(Flag.run_id, latest_run_ids(db, snapshot_id, detectors)))
        if detector is not None:
            query = query.where(Flag.detector == detector)
        if wallet_id is not None:
            query = query.where(Flag.wallet_id == wallet_id)
        return orm_rows(db, query.order_by(Flag.flag_id).limit(limit))


@app.get("/flags/{flag_id}/evidence")
def flag_evidence_endpoint(flag_id: int):
    """Returns whatever flag_evidence rows already exist for this flag --
    does NOT compute evidence on demand (that's graph/evidence.py's job,
    run offline/by the audit engine; a GET here stays fast and read-only).
    Empty list, not an error, if evidence hasn't been collected yet."""
    if using_mock():
        return load_mock().get("flag_evidence", {}).get(str(flag_id), [])
    with get_session() as db:
        return orm_rows(db, select(FlagEvidence.tx_hash, FlagEvidence.hop, FlagEvidence.block_number, FlagEvidence.role)
                        .where(FlagEvidence.flag_id == flag_id)
                        .order_by(FlagEvidence.block_number, FlagEvidence.hop))


@app.get("/flags/{flag_id}/evidence-edges")
def flag_evidence_edges_endpoint(flag_id: int):
    """flag_evidence rows joined to their transaction so a client can draw
    them: from_wallet_id is the SIGNER (transaction.from_wallet_id, never
    token_transfer.from_wallet_id). Empty list if no evidence exists."""
    if using_mock():
        return load_mock().get("flag_evidence_edges", {}).get(str(flag_id), [])
    signer, recipient = aliased(Wallet), aliased(Wallet)
    with get_session() as db:
        return orm_rows(db, select(FlagEvidence.tx_hash, FlagEvidence.hop, FlagEvidence.block_number, FlagEvidence.role,
                                   Transaction.from_wallet_id, Transaction.to_wallet_id,
                                   cast(Transaction.value_wei, Text).label("value_wei"),
                                   signer.address.label("from_address"), recipient.address.label("to_address"))
                        .select_from(FlagEvidence)
                        .join(FlagEvidence.transaction)
                        .join(Transaction.signer.of_type(signer))
                        .outerjoin(Transaction.recipient.of_type(recipient))   # contract creations have none
                        .where(FlagEvidence.flag_id == flag_id)
                        .order_by(FlagEvidence.block_number, FlagEvidence.hop))


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
    with get_session() as db:
        row = orm_row(db, select(FlagAudit.flag_audit_id, FlagAudit.flag_id, FlagAudit.verdict,
                                 FlagAudit.framing_cost_wei, FlagAudit.framing_gas, FlagAudit.minimal_tx_set,
                                 FlagAudit.method, FlagAudit.backend, FlagAudit.runtime_ms, FlagAudit.created_at)
                      .where(FlagAudit.flag_id == flag_id)
                      .order_by(FlagAudit.created_at.desc())
                      .limit(1))
        if row is None:
            raise HTTPException(404, "flag not audited yet")
        return row


@app.get("/audit/verdicts")
def audit_verdicts(snapshot_id: Optional[int] = None):
    """Latest audit per flag in one call (flags never audited are absent), so
    the dashboard doesn't probe /flags/{id}/audit once per row."""
    if using_mock():
        return [
            {k: a.get(k) for k in ("flag_id", "verdict", "framing_cost_wei", "method", "minimal_tx_set")}
            for a in load_mock().get("flag_audit", {}).values()
        ]
    with get_session() as db:
        # v_flag_latest_audit (migration 011): the newest audit per flag.
        la = FlagLatestAudit
        query = select(la.flag_id, la.verdict, cast(la.framing_cost_wei, Text).label("framing_cost_wei"),
                       la.method, la.minimal_tx_set)
        if snapshot_id is not None:
            query = query.where(in_run_ids(la.run_id, latest_run_ids(db, snapshot_id)))
        return orm_rows(db, query.order_by(la.flag_id))


@app.get("/audit/overview")
def audit_overview(snapshot_id: Optional[int] = None):
    """Headline FrameCheck metrics for the dashboard: flags per detector
    (audited or not), overall share frameable and the true median framing
    cost across every frameable flag (not a median of per-detector medians).
    Uses each flag's latest audit row and, with snapshot_id, only the latest
    run per detector on that snapshot."""
    if using_mock():
        return load_mock().get("audit_overview", {})
    with get_session() as db:
        run_ids = latest_run_ids(db, snapshot_id) if snapshot_id is not None else None
        la = FlagLatestAudit            # v_flag_latest_audit (migration 011)

        # From flag, LEFT JOIN the latest audit: unaudited flags still count.
        per_detector = orm_rows(db, select(
            Flag.detector,
            func.count().label("flag_count"),
            func.count(la.flag_id).label("audited_count"),
            func.count().filter(la.verdict == "frameable").label("frameable_count"),
        ).select_from(Flag).outerjoin(la, la.flag_id == Flag.flag_id)
            .where(*([in_run_ids(Flag.run_id, run_ids)] if run_ids is not None else []))
            .group_by(Flag.detector).order_by(Flag.detector))

        median = db.scalar(
            select(func.percentile_cont(0.5).within_group(la.framing_cost_wei))
            .where(*([in_run_ids(la.run_id, run_ids)] if run_ids is not None else []),
                   la.verdict == "frameable", la.framing_cost_wei.is_not(None)))
    flags = sum(r["flag_count"] for r in per_detector)
    audited = sum(r["audited_count"] for r in per_detector)
    frameable = sum(r["frameable_count"] for r in per_detector)
    return {
        "total_flags": flags,
        "total_audited": audited,
        "frameable_count": frameable,
        "frameable_share": frameable / audited if audited else 0.0,
        "framing_cost_median_wei": float(median) if median is not None else None,
        "per_detector": per_detector,
    }


# RAW SQL (allowed): calls the PL/pgSQL function flag_audit_chain_verify() (migration 009) with bound parameters
@app.get("/audit/trail")
def audit_trail():
    """Whether the audit chain still verifies, and what has been removed from it.

    Feature 2's headline: every flag_audit row is hash-chained to the one
    before it (migration 009), so editing a verdict after the fact breaks the
    chain at that row and at every row after it. This endpoint recomputes the
    whole chain in the database -- not in Python, which could be made to agree
    with whatever it was shown -- and reports the first row that does not fit.

    A chain that does not verify is not necessarily foul play: a restored
    backup or a hand-edited dump breaks it too. What it means is that the
    database can no longer vouch for those rows, which is exactly the thing
    an audit should be able to say out loud.
    """
    if using_mock():
        return load_mock().get("audit_trail", {
            "available": True, "rows_checked": 0, "intact": True,
            "first_bad_audit_id": None, "reason": None, "tombstones": [],
        })
    with get_conn() as conn:
        with conn.cursor() as cur:
            if not audit_trail_installed(cur):
                # The migration has not been applied here. Say so plainly
                # rather than reporting an intact chain that does not exist.
                return {"available": False,
                        "detail": "migration 009 has not been applied to this database, "
                                  "so audit rows are not chained yet"}
            cur.execute("SELECT * FROM flag_audit_chain_verify()")
            checked, intact, first_bad, reason, tombstone_count = cur.fetchone()
            cur.execute("""
                SELECT flag_audit_id, flag_id, verdict, deleted_by, deleted_at, reason
                  FROM flag_audit_tombstone
                 ORDER BY deleted_at DESC
                 LIMIT 50
            """)
            tombstones = [
                {"flag_audit_id": r[0], "flag_id": r[1], "verdict": r[2],
                 "deleted_by": r[3], "deleted_at": r[4].isoformat() if r[4] else None,
                 "reason": r[5]}
                for r in cur.fetchall()
            ]
            return {"available": True, "rows_checked": checked, "intact": intact,
                    "first_bad_audit_id": first_bad, "reason": reason,
                    "tombstone_count": tombstone_count, "tombstones": tombstones}


# RAW SQL (allowed): catalogue probe for migration 009's PL/pgSQL function
def audit_trail_installed(cur) -> bool:
    """Whether migration 009's functions exist in this database."""
    cur.execute("SELECT to_regprocedure('flag_audit_chain_verify()') IS NOT NULL")
    return bool(cur.fetchone()[0])


# RAW SQL (allowed): calls the PL/pgSQL function flag_versions() (migration 009) with bound parameters
@app.get("/flags/{flag_id}/versions")
def flag_versions(flag_id: int):
    """Every version this flag row has had, oldest first.

    A flag is a claim ("this wallet looks like it did X"), and an audit
    verdict is a claim about the flag AS IT STOOD. If the flag's score or
    threshold changed afterwards, the verdict refers to something that no
    longer exists -- so the history is part of reading the verdict, not a
    footnote to it.
    """
    if using_mock():
        versions = load_mock().get("flag_versions", {})
        return versions.get(str(flag_id), {"available": True, "flag_id": flag_id, "versions": []})
    with get_conn() as conn:
        with conn.cursor() as cur:
            if not audit_trail_installed(cur):
                return {"available": False, "flag_id": flag_id,
                        "detail": "migration 009 has not been applied to this database"}
            cur.execute("SELECT * FROM flag_versions(%s)", (flag_id,))
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail=f"no flag with id {flag_id}")
            return {"available": True, "flag_id": flag_id, "versions": [
                {"version": r[0],
                 "valid_from": r[1].isoformat() if r[1] else None,
                 "valid_to": r[2].isoformat() if r[2] else None,
                 "is_current": r[3],
                 "change_op": {"U": "updated", "D": "deleted", None: None}.get(r[4], r[4]),
                 "changed_by": r[5],
                 "row": r[6]}
                for r in rows
            ]}


def normalize_timestamp_arg(at: str) -> str:
    """Repairs the one way an otherwise-correct caller gets this wrong.

    An ISO timestamp with a numeric offset -- 2026-10-12T01:35:55+00:00 --
    has a '+' in it, and '+' in a URL query string decodes to a space. A
    caller who passes a perfectly good timestamp therefore arrives here with
    "2026-10-12T01:35:55 00:00", which Postgres rejects. Found by passing a
    real timestamp straight from flag_history into this endpoint.

    So a space followed by a UTC-offset-shaped tail is read back as the '+'
    it almost certainly was. Everything else is left exactly as given --
    including genuinely malformed input, which still earns its 422.
    """
    at = at.strip()
    split = re.fullmatch(r"(.*\d) (\d{2}:?\d{2})", at)
    return f"{split.group(1)}+{split.group(2)}" if split else at


# RAW SQL (allowed): calls the PL/pgSQL function flag_as_of() (migration 009) with bound parameters
@app.get("/flags/as-of")
def flags_as_of(at: str, limit: int = 50, detector: Optional[str] = None):
    """The flag table as it stood at an instant, straight out of flag_as_of().

    The reason this is a SQL function and not Python: reassembling history
    outside the database means every caller has to get the same reasoning
    right, and a caller that gets it wrong produces a plausible-looking past
    that never happened.
    """
    if using_mock():
        return {"available": True, "as_of": at, "flags": load_mock().get("flags", [])[:limit]}
    limit = max(1, min(limit, 500))
    at = normalize_timestamp_arg(at)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if not audit_trail_installed(cur):
                return {"available": False, "as_of": at,
                        "detail": "migration 009 has not been applied to this database"}
            query = """
                SELECT flag_id, run_id, wallet_id, detector, score, threshold, created_at
                  FROM flag_as_of(%s)
            """
            params: list = [at]
            if detector:
                query += " WHERE detector = %s"
                params.append(detector)
            query += " ORDER BY flag_id LIMIT %s"
            params.append(limit)
            try:
                cur.execute(query, params)
            except psycopg2.errors.InvalidDatetimeFormat:
                raise HTTPException(
                    status_code=422,
                    detail=f"{at!r} is not a timestamp -- try 2026-10-11T14:30:00Z") from None
            rows = [
                {"flag_id": r[0], "run_id": r[1], "wallet_id": r[2], "detector": r[3],
                 "score": r[4], "threshold": r[5],
                 "created_at": r[6].isoformat() if r[6] else None}
                for r in cur.fetchall()
            ]
            cur.execute("SELECT count(*) FROM flag_as_of(%s)", (at,))
            total = cur.fetchone()[0]
            return {"available": True, "as_of": at, "total": total, "flags": rows}


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
    with get_session() as db:
        la = FlagLatestAudit            # v_flag_latest_audit (migration 011)
        frameable = la.verdict == "frameable"
        query = select(
            la.detector,
            func.count().label("total_audited"),
            func.count().filter(frameable).label("frameable_count"),
            func.min(la.framing_cost_wei).filter(frameable).label("framing_cost_min_wei"),
            # percentile_cont returns double precision; type_coerce keeps it a
            # float in Python (the column it orders by is NUMERIC).
            type_coerce(func.percentile_cont(0.5).within_group(la.framing_cost_wei).filter(frameable),
                        DOUBLE_PRECISION).label("framing_cost_median_wei"),
            func.max(la.framing_cost_wei).filter(frameable).label("framing_cost_max_wei"),
        )
        if run_id is not None:
            query = query.where(la.run_id == run_id)
        elif snapshot_id is not None:
            query = query.where(in_run_ids(la.run_id, latest_run_ids(db, snapshot_id)))
        rows = orm_rows(db, query.group_by(la.detector).order_by(la.detector))
    for row in rows:
        row["frameable_share"] = row["frameable_count"] / row["total_audited"] if row["total_audited"] else 0.0
    return rows


class JobRequest(BaseModel):
    job_type: str
    snapshot_id: int
    seed_wallet_id: int
    detector: Optional[str] = None
    limit: Optional[int] = None


@app.post("/jobs")
def start_job(body: JobRequest):
    """Runs detectors or an audit pass without a terminal. Mutating, so it
    is behind the auth gate like everything else; it only ever inserts."""
    if using_mock():
        raise HTTPException(status_code=409, detail="analysis jobs need live mode (FRAMECHECK_API_MODE=live)")
    try:
        job = jobs.start(body.job_type, body.snapshot_id, body.seed_wallet_id,
                         detector=body.detector, limit=body.limit)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except RuntimeError as e:
        # Already running: a conflict, not a server error -- the UI shows
        # the running job rather than starting a second one.
        raise HTTPException(status_code=409, detail=str(e)) from e
    return job.as_dict()


def _without_secrets(job: dict) -> dict:
    """An ingest job carries the Etherscan key in its params so the crawl can
    authenticate. Every route that returns a job strips it -- the page asked
    for progress, not for the secret back."""
    if not job.get("params"):
        return job
    return {**job, "params": {k: v for k, v in job["params"].items() if k != "api_key"}}


@app.get("/jobs")
def list_jobs():
    return [_without_secrets(j) for j in jobs.list_jobs()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job (jobs are forgotten when the backend restarts)")
    return _without_secrets(job.as_dict())


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    if not jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail=f"job is already {job.status}")
    return jobs.get_job(job_id).as_dict()


@app.get("/flags/{flag_id}/audit.csv")
def flag_audit_csv(flag_id: int):
    """One flag's audit report as CSV. Rendered server-side so the file
    holds exactly what the database says, not what the UI happened to have
    loaded and formatted."""
    if using_mock():
        raise HTTPException(status_code=409, detail="audit export needs live mode")
    # The flag's newest audit, as a LATERAL subquery correlated on the flag.
    latest = (select(FlagAudit)
              .where(FlagAudit.flag_id == Flag.flag_id)
              .order_by(FlagAudit.created_at.desc(), FlagAudit.flag_audit_id.desc())
              .limit(1)
              .lateral("fa"))
    with get_session() as db:
        result = db.execute(
            select(Flag.flag_id, Flag.detector, Flag.wallet_id, Wallet.address, Flag.score, Flag.threshold,
                   Flag.token_contract, AnalysisRun.snapshot_id,
                   latest.c.verdict, latest.c.framing_cost_wei, latest.c.method, latest.c.backend,
                   latest.c.runtime_ms, latest.c.created_at)
            .select_from(Flag)
            .join(Flag.run)
            .outerjoin(Flag.wallet)
            .outerjoin(latest, true())
            .where(Flag.flag_id == flag_id))
        columns = list(result.keys())
        row = result.first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such flag")
        evidence = db.execute(
            select(FlagEvidence.tx_hash, FlagEvidence.hop, FlagEvidence.block_number, FlagEvidence.role)
            .where(FlagEvidence.flag_id == flag_id)
            .order_by(FlagEvidence.block_number, FlagEvidence.hop)).all()

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["section", *columns])
    writer.writerow(["audit", *["" if v is None else v for v in row]])
    if evidence:
        writer.writerow([])
        writer.writerow(["section", "tx_hash", "hop", "block_number", "role"])
        for ev in evidence:
            writer.writerow(["evidence", *ev])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="framecheck-flag-{flag_id}.csv"'},
    )


class GoogleCallback(BaseModel):
    id_token: str


# Swapped out in tests so the suite never reaches Google. Never a way to
# skip verification in a real run -- the default is the real check.
_verify_id_token = auth.verify_google_id_token


@app.get("/auth/login")
def auth_login():
    """PKCE parameters for the client to start the flow with. The verifier
    is returned to the caller because the caller is the one that must keep
    it and present it when exchanging the code -- that is what binds the
    code to this client."""
    client_id = auth.google_client_id()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this server")
    verifier, challenge = auth.new_pkce_pair()
    return {
        "client_id": client_id,
        "code_verifier": verifier,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "scope": "openid email profile",
    }


@app.post("/auth/callback")
def auth_callback(body: GoogleCallback, response: Response):
    """Verifies Google's ID token, upserts the user, and issues our own
    session. The browser gets it as an HttpOnly cookie (so page scripts
    cannot read it); the desktop app reads `session_token` from the body,
    because its webview and the sidecar do not share a cookie jar."""
    client_id = auth.google_client_id()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this server")
    try:
        claims = _verify_id_token(body.id_token, client_id)
    except auth.AuthError as e:
        log.info("Rejected Google ID token: %s", e)
        raise HTTPException(status_code=401, detail="sign-in failed") from e
    except Exception as e:
        log.warning("Google ID token verification error: %s", e)
        raise HTTPException(status_code=401, detail="sign-in failed") from e

    # Allowlist before any row is written: someone refused here leaves no
    # account behind.
    if not auth.email_allowed(claims.get("email", "")):
        log.info("Refused sign-in for an address not on FRAMECHECK_ALLOWED_EMAILS")
        raise HTTPException(status_code=403, detail="this account is not allowed to sign in here")
    with get_session() as db:
        user = auth.upsert_user(db, claims)
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="this account has been deactivated")
    return _signed_in(response, user)


def _signed_in(response: Response, user: dict) -> dict:
    """Issues our session for a verified user -- the same cookie and body
    whether they signed in with Google or a local password."""
    token = auth.issue_session(user)
    response.set_cookie(
        auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
        # secure=False: this backend is served over plain http on
        # localhost (and as a Tauri sidecar). Setting Secure here would
        # mean the cookie is never stored at all.
        secure=False,
    )
    return {"user": {k: user[k] for k in ("user_id", "email", "display_name", "picture_url")},
            "session_token": token}


class LocalLogin(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


@app.post("/auth/local/login")
def auth_local_login(body: LocalLogin, request: Request, response: Response):
    """Username + password sign-in for local accounts (bcrypt). Returns the
    same session as Google sign-in. One generic 401 for every failure, and at
    most 5 attempts a minute per client address and per username."""
    if using_mock():
        raise HTTPException(status_code=409, detail="local sign-in needs live mode")
    client = request.client.host if request.client else "unknown"
    if not auth.login_limiter.allow(f"ip:{client}", f"user:{body.username.lower()}"):
        raise HTTPException(status_code=429, detail="too many sign-in attempts -- wait a minute and try again",
                            headers={"Retry-After": "60"})
    try:
        with get_session() as db:
            user = auth.local_login(db, body.username, body.password)
    except auth.AuthError:
        raise HTTPException(status_code=401, detail=auth.LOGIN_FAILED) from None
    return _signed_in(response, user)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


@app.post("/auth/local/password")
def auth_local_password(body: PasswordChange, request: Request):
    """Change your own password: needs the current one; the new one must be
    at least 12 characters (and at most 72 bytes, bcrypt's limit)."""
    if using_mock():
        raise HTTPException(status_code=409, detail="changing a password needs live mode")
    signed_in = getattr(request.state, "session", None)
    if signed_in is None:
        raise HTTPException(status_code=409, detail="no signed-in user -- sign-in is disabled on this server")
    try:
        auth.validate_new_password(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not auth.login_limiter.allow(f"pw:{signed_in['user_id']}"):
        raise HTTPException(status_code=429, detail="too many attempts -- wait a minute and try again",
                            headers={"Retry-After": "60"})
    try:
        with get_session() as db:
            auth.change_password(db, signed_in["user_id"], body.current_password, body.new_password)
    except auth.AuthError:
        raise HTTPException(status_code=403, detail="current password is incorrect") from None
    return {"status": "password changed"}


@app.get("/auth/session")
def auth_session(request: Request):
    """Who am I? Public so the UI can ask before it has a session and get
    a plain answer instead of a 401 it has to special-case."""
    # `role` and `username` (additive) tell the UI which menus, pages and
    # buttons to offer. They are UX only: the middleware is the real gate.
    if auth.public_demo_mode():                 # before "disabled": see the gate
        return {"authenticated": True, "auth_mode": "public_demo", "user": None,
                "role": rbac.DEMO_VIEWER["role"], "username": None}
    if auth.auth_mode() == "disabled":
        return {"authenticated": True, "auth_mode": "disabled", "user": None,
                "role": rbac.LOCAL_ADMIN["role"], "username": None}
    token = request.cookies.get(auth.SESSION_COOKIE)
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        return {"authenticated": False, "auth_mode": "required", "user": None}
    try:
        session = auth.read_session(token)
    except auth.AuthError:
        return {"authenticated": False, "auth_mode": "required", "user": None}
    if using_mock():
        # No users table to read: a valid session is a viewer, as in the gate.
        return {"authenticated": True, "auth_mode": "required", "user": None,
                "role": rbac.VIEWER, "username": None}
    with get_session() as db:
        user = auth.pg_user_fetch(db, session["user_id"])
    if user is None or not user["is_active"]:
        return {"authenticated": False, "auth_mode": "required", "user": None}
    return {"authenticated": True, "auth_mode": "required",
            "user": {k: user[k] for k in ("user_id", "email", "display_name", "picture_url")},
            "role": user["role"], "username": user["username"]}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"status": "signed out"}


BENCHMARKS_PATH = pathlib.Path(__file__).parent.parent / "docs" / "perf" / "benchmarks.json"


@app.get("/benchmarks")
def benchmarks():
    """CPU-vs-GPU timings recorded by scripts/benchmark.py --json, plus the
    per-size crossover from scripts/benchmark_crossover.py. A file, not a
    table: these are measurements of this machine, not pipeline output, and
    they have to be readable with no Postgres at all. Empty `runs` when
    nothing has been benchmarked yet -- the dashboard shows that as an empty
    state, which is a real answer, not an error.

    Shape changed from a bare list to {"runs": [...], "crossover": {...}} so
    the crossover could travel with the timings; the dashboard accepts both
    so an older benchmarks.json still renders."""
    try:
        data = json.loads(BENCHMARKS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"runs": [], "crossover": None}
    except (json.JSONDecodeError, OSError):
        log.warning("Benchmark results at %s are unreadable", BENCHMARKS_PATH)
        return {"runs": [], "crossover": None}
    return {"runs": data.get("runs", []), "crossover": data.get("crossover")}


# --------------------------------------------------------------- Database view
# Feature 1: an installed app has no .env, no psql and no repo checkout, so
# everything needed to get from "Postgres is installed" to "FrameCheck has
# data" is reachable from the UI. See api/dbsetup.py for the operations and
# for what is deliberately absent (anything that drops or truncates).


class DbSettingsUpdate(BaseModel):
    host: str
    port: str = "5432"
    dbname: str
    user: str
    password: str = ""


@app.post("/internal/db-settings")
def set_internal_db_settings(body: DbSettingsUpdate, request: Request):
    """Loopback-only and internal-token-gated, like /internal/api-key: the
    password arrives from Rust's keychain, never from the page. Resets the
    pool, since pooled connections to the previous database would otherwise
    outlive the setting that made them."""
    token = request.headers.get("x-internal-token", "")
    expected = _runtime_state["internal_token"]
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid or missing internal token")
    try:
        redacted = dbsetup.set_runtime_dsn(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    close_pool()
    return {"status": "ok", "settings": redacted}


@app.get("/db/settings")
def db_settings():
    """Never includes the password -- only whether one is set, which the UI
    needs in order to say so."""
    return {"settings": dbsetup.redact(dbsetup.current_dsn()),
            "source": "app" if dbsetup.has_runtime_dsn() else "environment"}


@app.get("/db/status")
def db_status():
    """One call answering everything the Database view's header needs: is
    the server reachable, does the database exist, is the schema applied,
    which migrations are pending, and is the API serving live or demo data."""
    dsn = dbsetup.current_dsn()
    probe = dbsetup.server_probe(dsn)
    out = {
        "mode": "mock" if using_mock() else "live",
        "settings": dbsetup.redact(dsn),
        "server": probe,
        "schema": None,
    }
    if not probe["reachable"] or not probe["database_exists"]:
        return out
    try:
        conn = dbsetup.connect(dsn)
    except OperationalError as e:
        out["server"] = {**probe, "reachable": False, "error": str(e).strip()}
        return out
    try:
        out["schema"] = dbsetup.schema_state(conn)
    finally:
        conn.close()
    return out


@app.post("/db/create")
def db_create():
    """Creates the configured database if it is missing. An existing
    database is reported and left alone -- never recreated."""
    try:
        return dbsetup.create_database(dbsetup.current_dsn())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    except psycopg2.Error as e:
        raise HTTPException(status_code=400, detail=str(e).strip()) from None


@app.post("/db/schema")
def db_schema():
    """Applies schema.sql (only if absent) and every unapplied migration,
    each in its own transaction. Refuses a database whose migration history
    is unknown -- see /db/adopt."""
    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return dbsetup.apply_schema(conn)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except psycopg2.Error as e:
        raise HTTPException(status_code=400, detail=str(e).strip()) from None
    finally:
        conn.close()


@app.post("/db/adopt")
def db_adopt():
    """Records every migration as applied WITHOUT running it, for a database
    that predates migration tracking. A claim about history that cannot be
    checked from inside the database, so it is its own explicit action."""
    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return dbsetup.mark_existing_as_applied(conn)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    finally:
        conn.close()


@app.get("/db/tables")
def db_tables():
    """Every table with an estimated row count and on-disk size. Estimates
    because counting all 23 tables of a real FrameCheck database measured
    6.85 s; /db/tables/{table}/count gives the exact figure for one."""
    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return {"tables": dbsetup.table_inventory(conn)}
    finally:
        conn.close()


@app.get("/db/tables/{table}/count")
def db_table_count(table: str):
    """Exact count(*) for one table, for when the estimate is not enough."""
    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return dbsetup.exact_row_count(conn, table)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    finally:
        conn.close()


@app.get("/db/tables/{table}")
def db_table_sample(table: str, limit: int = 50):
    """Read-only peek at one table. The name is validated against the live
    catalogue and quoted as an identifier, never interpolated."""
    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return dbsetup.sample_table(conn, table, limit)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    finally:
        conn.close()


class DatasetImport(BaseModel):
    # "demo" imports the bundled dataset; "file" imports `path`. The page
    # never needs to know where the bundled one lives.
    source: str = "demo"
    path: Optional[str] = None


@app.get("/db/datasets")
def db_datasets():
    """What can be imported: the bundled demo dataset, if this build carries
    one. A source checkout that has never run scripts/export_dataset.py has
    no datasets/ directory, and the UI says so rather than offering a button
    that cannot work."""
    path = dbsetup.demo_dataset()
    if path is None:
        return {"demo": None}
    try:
        return {"demo": dbsetup.describe_dataset(path)}
    except (ValueError, OSError) as e:
        log.warning("bundled demo dataset at %s is unreadable: %s", path, e)
        return {"demo": None}


@app.post("/db/import")
def db_import(body: DatasetImport):
    """Loads a dataset into the configured database, in one transaction.

    Blocking rather than a background job: the bundled dataset is 444k rows
    and measured 8.5 s to import, which is worth waiting for in a request.
    A much larger dataset would deserve a job, and does not exist yet.
    """
    if body.source == "demo":
        path = dbsetup.demo_dataset()
        if path is None:
            raise HTTPException(status_code=404, detail="this build does not include a demo dataset")
    elif body.source == "file":
        if not body.path:
            raise HTTPException(status_code=422, detail="path is required when source is 'file'")
        path = pathlib.Path(body.path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {body.path}")
    else:
        raise HTTPException(status_code=422, detail="source must be 'demo' or 'file'")

    try:
        conn = dbsetup.connect(dbsetup.current_dsn())
    except OperationalError as e:
        raise HTTPException(status_code=503, detail=f"database unavailable: {str(e).strip()}") from None
    try:
        return dbsetup.import_dataset(conn, path)
    except ValueError as e:
        # Not one of our dataset files -- refused before anything was written.
        raise HTTPException(status_code=422, detail=str(e)) from None
    except RuntimeError as e:
        # Target not empty, or the file wants a table it may not write.
        raise HTTPException(status_code=409, detail=str(e)) from None
    except psycopg2.Error as e:
        raise HTTPException(status_code=400, detail=str(e).strip()) from None
    finally:
        conn.close()


class ModeUpdate(BaseModel):
    mode: str


@app.post("/db/mode")
def db_mode(body: ModeUpdate):
    """Switches between demo (mock) and live data without a restart. Live is
    refused unless the database is actually reachable with the schema
    applied: silently switching to live and then failing every subsequent
    request would look like the app breaking."""
    global _mode_override
    mode = body.mode.lower().strip()
    if mode not in ("mock", "live"):
        raise HTTPException(status_code=422, detail="mode must be 'mock' or 'live'")
    if mode == "live":
        status = db_status()
        if not status["server"]["reachable"]:
            raise HTTPException(status_code=409,
                                detail=f"cannot switch to live: {status['server']['error']}")
        if not status["server"]["database_exists"]:
            raise HTTPException(status_code=409,
                                detail="cannot switch to live: the database does not exist yet")
        if not (status["schema"] or {}).get("schema_applied"):
            raise HTTPException(status_code=409,
                                detail="cannot switch to live: the schema has not been applied yet")
    _mode_override = mode
    close_pool()
    log.info("API mode switched to %s", mode)
    return {"mode": mode}


# ------------------------------------------------------- automatic ingestion
# The other half of Feature 1: with an Etherscan key saved, the app pulls
# live wallet data itself instead of the user running `python -m
# ingestion.ingest_subgraph` in a terminal they do not have.


class IngestRequest(BaseModel):
    address: Optional[str] = None            # defaults to the configured case study
    hops: int = 1
    requests_per_second: Optional[float] = None
    max_calls: Optional[int] = None          # hard stop, not a rate -- see jobs._run_ingest


@app.get("/ingest/plan")
def ingest_plan():
    """What we know about this key's allowance, and how we know it.

    `source` is the honest part. Etherscan only reports an allowance for
    paid keys, so for most users this is `"configured"` -- the rate the app
    is using, not a plan it detected. It is never presented as detected
    when it was not.
    """
    key = _runtime_state["etherscan_api_key"]
    plan = {
        "api_key_set": bool(key),
        "requests_per_second": float(config.REQUESTS_PER_SECOND),
        "source": "configured",
        "reported_by_etherscan": None,
    }
    if not key:
        return plan
    try:
        reported = EtherscanClient(api_key=key).get_api_limit()
    except Exception as e:                      # a plan probe must never break the page
        log.info("API limit probe failed: %s", e)
        reported = None
    if reported:
        plan["reported_by_etherscan"] = reported
        plan["source"] = "etherscan"
    return plan


@app.post("/ingest/start")
def ingest_start(body: IngestRequest):
    """Starts a crawl as a background job. Live mode only -- there is no
    demo-mode equivalent of writing real wallets to a database."""
    if using_mock():
        raise HTTPException(status_code=409,
                            detail="ingestion needs live mode: connect a database in the Database view first")
    if not _runtime_state["etherscan_api_key"]:
        raise HTTPException(status_code=409, detail="no Etherscan API key is set -- add one in Settings first")
    if body.hops < 0 or body.hops > 3:
        # 3 hops out of a busy address is already hundreds of thousands of
        # calls; a bigger number is a mistake, not a request.
        raise HTTPException(status_code=422, detail="hops must be between 0 and 3")
    if body.max_calls is not None and body.max_calls < 1:
        raise HTTPException(status_code=422, detail="max_calls must be at least 1")
    try:
        job = jobs.start("ingest", snapshot_id=0, seed_wallet_id=0, params={
            "address": body.address,
            "hops": body.hops,
            "requests_per_second": body.requests_per_second,
            "max_calls": body.max_calls,
            "api_key": _runtime_state["etherscan_api_key"],
        })
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _without_secrets(job.as_dict())


# ── User management (admin) ────────────────────────────────────────────────

USER_COLUMNS = (AppUser.user_id, AppUser.username, AppUser.email, AppUser.display_name, AppUser.role,
                AppUser.is_active, AppUser.organisation, AppUser.created_at, AppUser.last_login_at)

# What the procedures in migration 011 raise, as HTTP. Anything else is a real
# fault and goes on to the generic handler.
PROCEDURE_ERRORS = {
    "42501": 403,       # actor is not an active admin
    "22023": 422,       # invalid role
    "P0002": 404,       # no such user
    "23001": 409,       # would remove the last active admin
    "23514": 409,       # app_user_can_sign_in: the account has no way to sign in
}
NO_WAY_TO_SIGN_IN = "this account has no way to sign in (no Google identity, no password), so it cannot be activated"


def _procedure_error(error: sa_exc.DBAPIError) -> HTTPException:
    code = getattr(error.orig, "pgcode", None)
    if code not in PROCEDURE_ERRORS:
        raise error
    detail = NO_WAY_TO_SIGN_IN if code == "23514" else error.orig.diag.message_primary
    return HTTPException(status_code=PROCEDURE_ERRORS[code], detail=detail)


def _admin_actor(request: Request) -> int:
    """The signed-in admin making a change. The procedures check their actor
    against app_user, and the local admin of a sign-in-disabled server has no
    row there -- so changing users needs a real signed-in account."""
    if using_mock():
        raise HTTPException(status_code=409, detail="user management needs live mode")
    user_id = getattr(request.state, "user", {}).get("user_id")
    if user_id is None:
        raise HTTPException(status_code=409, detail="changing users needs a signed-in admin, and sign-in is "
                                                    "disabled on this server (FRAMECHECK_AUTH_MODE=disabled)")
    return user_id


@app.get("/admin/users")
def admin_list_users(request: Request, response: Response, q: Optional[str] = Query(None, max_length=120),
                     page: Optional[int] = None, page_size: Optional[int] = None):
    """Every account, paginated, optionally searched by email or username.
    Never includes password hashes: USER_COLUMNS does not select them."""
    if using_mock():
        return pagination.page_of_list([], request, response, page, page_size)
    query = select(*USER_COLUMNS).order_by(AppUser.user_id)
    if q:
        query = query.where(or_(AppUser.email.icontains(q, autoescape=True),
                                AppUser.username.icontains(q, autoescape=True)))
    with get_session() as db:
        return pagination.page_of_query(db, query, request, response, page, page_size)


class UserUpdate(BaseModel):
    role: Optional[Literal["admin", "analyst", "viewer"]] = None
    is_active: Optional[bool] = None


@app.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, request: Request):
    """Change a role and/or activation, through the stored procedures, which
    hold the rules: the actor must be an active admin, and the last active
    admin can be neither demoted nor deactivated."""
    actor = _admin_actor(request)
    if body.role is None and body.is_active is None:
        raise HTTPException(status_code=422, detail="nothing to change: send role and/or is_active")
    try:
        with get_session() as db:
            if body.role is not None:
                # RAW SQL (allowed): stored procedure, bound parameters
                db.execute(text("CALL sp_set_user_role(:actor, :target, :role)"),
                           {"actor": actor, "target": user_id, "role": body.role})
            if body.is_active is not None:
                # RAW SQL (allowed): stored procedure, bound parameters
                db.execute(text("CALL sp_set_user_active(:actor, :target, :active)"),
                           {"actor": actor, "target": user_id, "active": body.is_active})
            return orm_row(db, select(*USER_COLUMNS).where(AppUser.user_id == user_id))
    except sa_exc.DBAPIError as e:
        raise _procedure_error(e) from None


@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    """A soft delete: users are referenced by analysis runs, tags and alert
    reviews, so the row stays and is_active goes false."""
    actor = _admin_actor(request)
    try:
        with get_session() as db:
            # RAW SQL (allowed): stored procedure, bound parameters
            db.execute(text("CALL sp_set_user_active(:actor, :target, FALSE)"),
                       {"actor": actor, "target": user_id})
    except sa_exc.DBAPIError as e:
        raise _procedure_error(e) from None
    return {"status": "deactivated", "user_id": user_id}


# ── Labels, wallet tags, alert review ──────────────────────────────────────
# All through ORM objects, one transaction per request (get_session commits on
# the way out). 404 for unknown ids, 409 for conflicts, 422 for bad input.

def _needs_live(what: str) -> None:
    if using_mock():
        raise HTTPException(status_code=409, detail=f"{what} needs live mode")


def _current_user_id(request: Request) -> Optional[int]:
    """Who to record as tagger/reviewer. The local admin of a sign-in-disabled
    server has no app_user row, so these stay NULL there (both are nullable)."""
    return getattr(request.state, "user", {}).get("user_id")


LABEL_COLUMNS = (Label.label_id, Label.label_name, Label.category, Label.source, Label.is_sanctioned)


def _label_dict(label: Label) -> dict:
    return {c.key: getattr(label, c.key) for c in LABEL_COLUMNS}


class LabelCreate(BaseModel):
    label_name: str = Field(min_length=1, max_length=60)
    category: Optional[str] = Field(None, max_length=30)
    source: Optional[str] = Field(None, max_length=40)
    is_sanctioned: bool = False


class LabelUpdate(BaseModel):
    label_name: Optional[str] = Field(None, min_length=1, max_length=60)
    category: Optional[str] = Field(None, max_length=30)
    source: Optional[str] = Field(None, max_length=40)
    is_sanctioned: Optional[bool] = None


@app.get("/labels")
def list_labels(request: Request, response: Response, q: Optional[str] = Query(None, max_length=60),
                category: Optional[str] = Query(None, max_length=30),
                page: Optional[int] = None, page_size: Optional[int] = None):
    if using_mock():
        rows = [row for row in load_mock().get("labels", [])
                if (not q or q.lower() in row["label_name"].lower())
                and (not category or row.get("category") == category)]
        return pagination.page_of_list(rows, request, response, page, page_size)
    query = select(*LABEL_COLUMNS).order_by(Label.label_id)
    if q:
        query = query.where(Label.label_name.icontains(q, autoescape=True))
    if category:
        query = query.where(Label.category == category)
    with get_session() as db:
        return pagination.page_of_query(db, query, request, response, page, page_size)


@app.get("/labels/{label_id}")
def get_label(label_id: int):
    if using_mock():
        for row in load_mock().get("labels", []):
            if row["label_id"] == label_id:
                return row
        raise HTTPException(404, "label not found")
    with get_session() as db:
        label = db.get(Label, label_id)
        if label is None:
            raise HTTPException(404, "label not found")
        return _label_dict(label)


def _duplicate_label(error: sa_exc.IntegrityError) -> HTTPException:
    if getattr(error.orig, "pgcode", None) != "23505":
        raise error
    return HTTPException(status_code=409, detail="a label with that name already exists")


@app.post("/labels", status_code=201)
def create_label(body: LabelCreate):
    _needs_live("creating a label")
    try:
        with get_session() as db:
            label = Label(**body.model_dump())
            db.add(label)
            db.flush()
            return _label_dict(label)
    except sa_exc.IntegrityError as e:
        raise _duplicate_label(e) from None


@app.patch("/labels/{label_id}")
def update_label(label_id: int, body: LabelUpdate):
    _needs_live("changing a label")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="nothing to change")
    if changes.get("label_name", "") is None or ("is_sanctioned" in changes and changes["is_sanctioned"] is None):
        raise HTTPException(status_code=422, detail="label_name and is_sanctioned cannot be null")
    try:
        with get_session() as db:
            label = db.get(Label, label_id)
            if label is None:
                raise HTTPException(404, "label not found")
            for field, value in changes.items():
                setattr(label, field, value)
            db.flush()
            return _label_dict(label)
    except sa_exc.IntegrityError as e:
        raise _duplicate_label(e) from None


@app.delete("/labels/{label_id}")
def delete_label(label_id: int, force: bool = False):
    """409 while the label is on any wallet, so a tag is never lost by
    accident; ?force=true detaches it first, in the same transaction."""
    _needs_live("deleting a label")
    with get_session() as db:
        label = db.get(Label, label_id)
        if label is None:
            raise HTTPException(404, "label not found")
        attached = db.scalar(select(func.count()).select_from(WalletLabel).where(WalletLabel.label_id == label_id))
        if attached and not force:
            raise HTTPException(status_code=409, detail=f"this label is attached to {attached} wallet(s); "
                                                        "pass force=true to detach it and delete")
        if attached:
            db.execute(delete(WalletLabel).where(WalletLabel.label_id == label_id))
        db.delete(label)
    return {"status": "deleted", "label_id": label_id, "detached": attached}


class WalletTag(BaseModel):
    label_id: int
    confidence: Optional[float] = Field(None, ge=0, le=1)


WALLET_TAG_COLUMNS = (WalletLabel.label_id, Label.label_name, Label.category, Label.is_sanctioned,
                      WalletLabel.confidence, WalletLabel.tagged_by, WalletLabel.tagged_at)


def _wallet_tags(wallet_id: int):
    return (select(*WALLET_TAG_COLUMNS).select_from(WalletLabel).join(WalletLabel.label)
            .where(WalletLabel.wallet_id == wallet_id).order_by(Label.label_name))


@app.get("/wallets/{wallet_id}/labels")
def wallet_labels(wallet_id: int):
    """A wallet's labels. Its own endpoint because GET /wallets/{id} keeps its
    body shape (the API contract is additive only)."""
    if using_mock():
        return []
    with get_session() as db:
        return orm_rows(db, _wallet_tags(wallet_id))


@app.post("/wallets/{wallet_id}/labels", status_code=201)
def tag_wallet(wallet_id: int, body: WalletTag, request: Request):
    _needs_live("tagging a wallet")
    with get_session() as db:
        if db.get(Wallet, wallet_id) is None:
            raise HTTPException(404, "wallet not found")
        if db.get(Label, body.label_id) is None:
            raise HTTPException(404, "label not found")
        if db.get(WalletLabel, (wallet_id, body.label_id)) is not None:
            raise HTTPException(status_code=409, detail="this wallet already has that label")
        db.add(WalletLabel(wallet_id=wallet_id, label_id=body.label_id, confidence=body.confidence,
                           tagged_by=_current_user_id(request)))
        db.flush()
        return orm_row(db, _wallet_tags(wallet_id).where(WalletLabel.label_id == body.label_id))


@app.delete("/wallets/{wallet_id}/labels/{label_id}")
def untag_wallet(wallet_id: int, label_id: int):
    _needs_live("removing a wallet's label")
    with get_session() as db:
        tag = db.get(WalletLabel, (wallet_id, label_id))
        if tag is None:
            raise HTTPException(404, "this wallet does not have that label")
        db.delete(tag)
    return {"status": "removed", "wallet_id": wallet_id, "label_id": label_id}


class AlertReview(BaseModel):
    review_status: Literal["NEW", "IN_REVIEW", "CONFIRMED", "DISMISSED"]


@app.patch("/alerts/{alert_id}")
def review_alert(alert_id: int, body: AlertReview, request: Request):
    """Sets the alert's status and records who reviewed it."""
    _needs_live("reviewing an alert")
    with get_session() as db:
        alert = db.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(404, "alert not found")
        alert.review_status = body.review_status
        alert.reviewed_by = _current_user_id(request)
        db.flush()
        return {**{c.key: getattr(alert, c.key) for c in ALERT_COLUMNS}, "reviewed_by": alert.reviewed_by}


# ── Serving the frontend from this same process ────────────────────────────
# So a public tunnel (ngrok/Cloudflare, see README's "Sharing a public demo
# link") pointed at this one port serves both the API and the pages -- no
# second server, no CORS, no separate URL to hand someone. Mounted LAST and
# deliberately at "/": Starlette matches routes in registration order and a
# Mount only catches what nothing earlier already matched, so every
# @app.get/@app.post above (and FastAPI's own /docs, /openapi.json, /redoc,
# added at app construction, before any of this file's own routes) keeps
# working exactly as it did -- confirmed by the test suite hitting both an
# API path and a static one. html=True serves frontend/index.html at "/" and
# any other file (frontend/dashboard.html, etc.) at its own path.
#
# Guarded by existence: the desktop app's frozen sidecar never bundles
# frontend/ (Tauri serves it directly from its own bundle, see
# scripts/build_backend_sidecar.py's --add-data list) -- mounting a missing
# directory unconditionally would crash that backend at import time, which
# is exactly the regression this guard exists to prevent.
FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
