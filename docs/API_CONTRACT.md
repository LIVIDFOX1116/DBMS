# API Contract

This is one of the three things locked between the two halves of the project
(project bible, Section 7.2). Changing an endpoint shape here requires both
of you to agree — the frontend is built against exactly this.

Base URL (local dev): `http://localhost:8000`

Run it: `uvicorn api.main:app --reload --port 8000` (see README.md for setup).
Interactive docs are free from FastAPI at `http://localhost:8000/docs`.

Two modes, same shapes, switched with one env var (`TXGRAPH_API_MODE=mock|live`,
default `mock`). The partner should build against **mock** for all of Weeks 1–4
and only needs `live` once the backend actually says it's ready (Phase 3.5 in
the beat sheet).

## GET /health
`{"status": "ok", "mode": "mock" | "live"}`

## GET /wallets?limit=50
List of wallets, most active first.
```json
[{"wallet_id": 1, "address": "0xc8a6...", "address_type": "EOA", "risk_score": 91.0, "tx_count": 42}]
```

## GET /wallets/{wallet_id}
Single wallet, same shape as above. `404` if not found.

## GET /clusters?run_id=1 (optional)
```json
[{"cluster_id": 1, "run_id": 1, "community_label": 0, "member_count": 3,
  "cluster_type": "SYBIL", "risk_score": 72.5, "modularity_score": 0.41}]
```
`cluster_type` is one of `SYBIL`, `WASH_TRADE`, `MIXER`, `PEEL_CHAIN`, or `null`
if the run hasn't been triaged into a type yet — always handle `null` in the UI.

## GET /clusters/{cluster_id}
Single cluster, same shape as one `/clusters` list item. `404` if not found.
Needed because `/clusters` is capped (see below) -- a real run can produce
thousands of clusters, so a specific cluster_id (e.g. from an alert) isn't
guaranteed to be in the capped list and needs its own lookup.

## GET /clusters/{cluster_id}/members
```json
[{"wallet_id": 2, "address": "0x1111...", "membership_score": 0.88, "is_core_node": true}]
```

## GET /alerts?status=NEW (optional; NEW | REVIEWED | DISMISSED)
```json
[{"alert_id": 1, "run_id": 1, "cluster_id": 1, "wallet_id": 2,
  "pattern_type": "SYBIL_FANOUT", "severity": "HIGH", "confidence": 0.74,
  "review_status": "NEW"}]
```
`cluster_id` and `wallet_id` are both nullable — an alert can be wallet-only,
cluster-only, or both.

## GET /snapshots
```json
[{"snapshot_id": 1, "snapshot_name": "polynetwork_2hop_v1", "start_block": 0,
  "end_block": 999999999, "vertex_count": 6, "edge_count": 7, "is_directed": true}]
```

## GET /snapshots/{snapshot_id}/edges?limit=500
The graph itself -- what the frontend's graph view renders as nodes (from
`/wallets`) and edges (from here).
```json
[{"src_wallet_id": 1, "dst_wallet_id": 2, "edge_weight": 12.4, "transfer_count": 5}]
```

## GET /runs/{run_id}/metrics?metric_name=pagerank&limit=50
`metric_name` is whichever algorithm wrote it: `pagerank`, or
`bfs_distance_from_{seed_wallet_id}` for path-tracing. There is no fixed enum
for `metric_name` — treat it as an open string and let the run's own metadata
tell you what's available. Louvain output comes from `/clusters`, not this
endpoint.
```json
[{"wallet_id": 1, "metric_value": 0.412, "rank_position": 1}]
```

## Known simplifications (v1, intentional)

- No auth. Fine for a local, single-machine student project; do not expose
  this publicly as-is.
- No pagination cursor, just `limit` (and for `/clusters`, a hard 100-row cap
  when `run_id` isn't given). This assumption didn't survive contact with
  real data: a single real crawl produced 9,531 clusters, so `/clusters`'
  unfiltered list is a somewhat arbitrary 100 of them, not a representative
  sample. `/clusters/{cluster_id}` exists specifically so a *known* cluster
  id (e.g. from an alert) can always be fetched regardless of whether it
  made that cut. Proper pagination is still the right long-term fix.
- `/runs/{run_id}/metrics` doesn't validate that `run_id` exists in mock mode;
  it just returns `[]`. The frontend should treat an empty list as "no data
  yet," not as an error.
- CORS is wide open (`allow_origins=["*"]`, GET only) so the frontend can be
  opened as a static file or served from any local port without config --
  safe only because there's no auth and nothing here mutates state.
