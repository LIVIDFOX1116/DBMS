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
import os
import pathlib
import secrets
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from pydantic import BaseModel

from ingestion import config

app = FastAPI(
    title="Transaction Graph Analyzer API",
    version="0.1.0",
    description="Shared contract between the GPU/backend half and the frontend half. See docs/API_CONTRACT.md.",
)

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


def get_conn():
    return psycopg2.connect(**config.PG_DSN)


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


@app.get("/wallets")
def list_wallets(limit: int = 50):
    if using_mock():
        return load_mock()["wallets"][:limit]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wallet_id, address, address_type, risk_score, tx_count
                   FROM wallet ORDER BY tx_count DESC NULLS LAST LIMIT %s""",
                (limit,),
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()


@app.get("/wallets/{wallet_id}")
def get_wallet(wallet_id: int):
    if using_mock():
        for w in load_mock()["wallets"]:
            if w["wallet_id"] == wallet_id:
                return w
        raise HTTPException(404, "wallet not found")
    conn = get_conn()
    try:
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
    finally:
        conn.close()


@app.get("/clusters")
def list_clusters(run_id: Optional[int] = None):
    if using_mock():
        clusters = load_mock()["clusters"]
        return [{k: v for k, v in c.items() if k != "members"} for c in clusters]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if run_id is not None:
                cur.execute(
                    """SELECT cluster_id, run_id, community_label, member_count, cluster_type, risk_score, modularity_score
                       FROM cluster WHERE run_id = %s""",
                    (run_id,),
                )
            else:
                cur.execute(
                    """SELECT cluster_id, run_id, community_label, member_count, cluster_type, risk_score, modularity_score
                       FROM cluster ORDER BY run_id DESC LIMIT 100"""
                )
            return rows_as_dicts(cur)
    finally:
        conn.close()


@app.get("/clusters/{cluster_id}")
def get_cluster(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return {k: v for k, v in c.items() if k != "members"}
        raise HTTPException(404, "cluster not found")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT cluster_id, run_id, community_label, member_count, cluster_type, risk_score, modularity_score
                   FROM cluster WHERE cluster_id = %s""",
                (cluster_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "cluster not found")
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    finally:
        conn.close()


@app.get("/clusters/{cluster_id}/members")
def cluster_members(cluster_id: int):
    if using_mock():
        for c in load_mock()["clusters"]:
            if c["cluster_id"] == cluster_id:
                return c.get("members", [])
        raise HTTPException(404, "cluster not found")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT cm.wallet_id, w.address, cm.membership_score, cm.is_core_node
                   FROM cluster_member cm JOIN wallet w ON w.wallet_id = cm.wallet_id
                   WHERE cm.cluster_id = %s""",
                (cluster_id,),
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()


@app.get("/alerts")
def list_alerts(status: Optional[str] = None):
    if using_mock():
        alerts = load_mock()["alerts"]
        return [a for a in alerts if status is None or a["review_status"] == status]
    conn = get_conn()
    try:
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
    finally:
        conn.close()


@app.get("/snapshots")
def list_snapshots():
    if using_mock():
        return load_mock()["snapshots"]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT snapshot_id, snapshot_name, start_block, end_block, vertex_count, edge_count, is_directed
                   FROM graph_snapshot ORDER BY created_at DESC"""
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()


@app.get("/snapshots/{snapshot_id}/edges")
def snapshot_edges(snapshot_id: int, limit: int = 500):
    if using_mock():
        edges = load_mock()["edges"].get(str(snapshot_id), [])
        return edges[:limit]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT src_wallet_id, dst_wallet_id, edge_weight, transfer_count
                   FROM snapshot_edge WHERE snapshot_id = %s LIMIT %s""",
                (snapshot_id, limit),
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()


@app.get("/runs/{run_id}/metrics")
def run_metrics(run_id: int, metric_name: str = "pagerank", limit: int = 50):
    if using_mock():
        run = load_mock()["runs"].get(str(run_id), {})
        return run.get(metric_name, [])[:limit]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wallet_id, metric_value, rank_position FROM vertex_metric
                   WHERE run_id = %s AND metric_name = %s ORDER BY rank_position LIMIT %s""",
                (run_id, metric_name, limit),
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()
