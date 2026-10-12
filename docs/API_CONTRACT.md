# API Contract

This is the interface locked between the two branches: `dev` (backend —
Python sidecar API, Etherscan ingestion, persistence, alerts, and the
non-UI Rust in `src-tauri` — graph algorithms, GPU compute, Tauri commands)
and `app` (UI/UX). Changing a shape here requires agreement from both
sides — the UI is built against exactly this.

Two halves:
- **HTTP endpoints** — served by the Python sidecar (`api/main.py`), called
  both directly (dev/browser preview) and by Rust (`src-tauri/src/commands.rs`
  proxies some of these for its own Tauri commands).
- **Tauri commands** — invoked from the webview via
  `window.__TAURI__.core.invoke(...)` (global script, no `@tauri-apps/api`
  npm package — this repo has no Node/npm anywhere, deliberately).

Compatibility rule: additive only. Add fields; never rename, remove, or
retype an existing one. Keep status codes and error shapes the frontend
already handles. A breaking change stops and asks first.

**Unexpected errors (any endpoint):** a genuinely unexpected server error
(a DB blowing up, a bug) returns `500 {"detail": "internal server error"}` --
never the real exception text, SQL, or a traceback. This is a stable,
documented shape as of the connection-pool/error-handling hardening pass;
previously this case was unhandled and its response was whatever
Starlette's default happened to produce. The real error is logged
server-side and viewable via the `get_backend_logs` Tauri command, not
returned to the caller.

---

## HTTP endpoints (Python sidecar, `api/main.py`)

Base URL (local dev): `http://localhost:8000` (or whatever port the Tauri
shell assigns the sidecar at spawn — see `get_api_base` below).

Run it standalone: `uvicorn api.main:app --reload --port 8000`. Interactive
OpenAPI docs are free from FastAPI at `http://localhost:8000/docs`.

Two modes, **identical shapes** in both, switched by one env var:
`FRAMECHECK_API_MODE=mock|live` (default `mock`). Mock serves
`api/mock_data.json`; live queries the real Postgres schema
(`db/schema.sql`). All examples below are real responses captured from the
live database on `snapshot_id=3` / cluster run `run_id=12` / metrics runs
`run_id=11` (pagerank) and `run_id=10` (BFS) — not fabricated placeholders.

CORS: `allow_origins=["*"]`, `GET` only. Safe only because there's no auth
and nothing here mutates state except `/internal/api-key` (POST, loopback +
token-gated — see below).

### GET /health
```json
{"status": "ok", "mode": "mock" | "live"}
```

### GET /stats/overview
True totals, unaffected by any other endpoint's row cap -- use this for
"how many wallets/clusters/alerts are there," never `.length` of a
`/wallets`, `/clusters`, or `/alerts` response (all three cap their
unfiltered result: 50-by-default/200-by-request, 100, and 100
respectively).
```json
{"wallet_count": 52581, "cluster_count": 70254, "alert_count": 197}
```

### GET /wallets?limit=50
List of wallets, most active (`tx_count`) first, `NULL`s last.
```json
[{"wallet_id": 425311, "address": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
  "address_type": "EOA", "risk_score": null, "tx_count": 11883}]
```
`address_type` is one of `EOA`, `CONTRACT` (as reclassified by
`ingestion.ingest_subgraph.reclassify_address_types`). `risk_score` is
nullable — real data currently has it unset (`null`) for every wallet; do
not assume it's always populated.

### GET /wallets/{wallet_id}
Single wallet, same shape as one `/wallets` list item.
Errors: `404 {"detail": "wallet not found"}` if `wallet_id` doesn't exist.
Note: wallet ids are sparse (real DB ids look like `425311`, `1502`, not a
dense 1..N range) — a UI must never assume small integer ids exist.

### GET /clusters?run_id=12 (optional)
```json
[{"cluster_id": 28596, "run_id": 12, "community_label": 2, "member_count": 1,
  "cluster_type": null, "risk_score": null, "modularity_score": 0.7258998477807098,
  "analysis_backend": "pandas+networkx (CPU)", "library_version": null}]
```
`cluster_type` is one of `SYBIL`, `WASH_TRADE`, `MIXER`, `PEEL_CHAIN`, or
`null` if the run hasn't been triaged into a type yet — always handle
`null` in the UI. When `run_id` is given, **all** clusters for that run are
returned (a real run can have thousands — 9,531 measured on the current
case-study data, no cap applies in this branch). When `run_id` is omitted,
the list is capped at 100 rows (`ORDER BY run_id DESC LIMIT 100`) and is
**not** representative — use `run_id` or `/clusters/{cluster_id}` instead
of relying on the unfiltered list.

`analysis_backend`/`library_version` (added — see `docs/UI_HANDOFF.md`):
which backend/library version produced this run's clusters
(`"pandas+networkx (CPU)"` or `"cudf+cugraph (GPU)"`, and e.g. a NetworkX
or cuGraph version string). Both nullable — mock mode and every run
recorded before this field existed (real data: every run so far) have
`null` here. Matters because cuGraph Louvain and NetworkX Louvain are not
guaranteed to produce identical partitions on the same data (see
`graph/backend.py`'s module docstring).

### GET /clusters/{cluster_id}
Single cluster, same shape as one `/clusters` list item (including
`analysis_backend`/`library_version`).
Errors: `404 {"detail": "cluster not found"}`.
Exists specifically because `/clusters` (unfiltered) is capped at 100 — a
specific cluster_id (e.g. from an alert) isn't guaranteed to be in that cut
and needs its own lookup.

### GET /cluster-membership?cluster_ids=32769,28596
`wallet_id -> [cluster_id, ...]` for the given clusters, in one round trip.
```json
{"1509": [32769], "1523": [32769], "1525": [32769]}
```
Replaces an N-request-per-cluster loop (100 requests on every dashboard
load against real data — ~48s against a real DB; invisible against mock's
single cluster). JSON object keys are always strings even though
`wallet_id` is numeric everywhere else — callers must `Number()`/`int()`
the keys back. The array is defensive schema generality: real Louvain
output (`graph/run_analysis.py`) assigns each wallet to exactly one
community, so in practice every array has exactly one element — but don't
hard-code that assumption away, the schema technically allows more.
Errors: `422 {"detail": "cluster_ids must be a comma-separated list of integers"}`
on malformed input. Empty `cluster_ids` (empty string, or none matching)
returns `{}`, not an error.

### GET /clusters/{cluster_id}/members
```json
[{"wallet_id": 428147, "address": "0x04fa0d235c4abf4bcf4787af4cf447de572ef828",
  "membership_score": null, "is_core_node": false}]
```
Errors: `404 {"detail": "cluster not found"}`.

### GET /alerts?status=NEW (optional; NEW | REVIEWED | DISMISSED)
```json
[{"alert_id": 105, "run_id": 12, "cluster_id": 37990, "wallet_id": null,
  "pattern_type": "SYBIL_FANOUT", "severity": "MEDIUM", "confidence": 0.57,
  "review_status": "NEW"}]
```
`cluster_id` and `wallet_id` are both nullable — an alert can be
wallet-only, cluster-only, or both; the real data currently produces
cluster-only alerts (`wallet_id: null`) exclusively. No `status` filter
returns everything, `ORDER BY detected_at DESC LIMIT 100`.

### GET /snapshots
```json
[{"snapshot_id": 3, "snapshot_name": "pipeline_20260830T125156", "start_block": 0,
  "end_block": 999999999, "vertex_count": 19119, "edge_count": 35756, "is_directed": true}]
```
Ordered newest-first (`created_at DESC`). All three real snapshots
currently share the same 19,119-vertex/35,756-edge case-study graph
(different pipeline runs over the same underlying crawl).

### GET /snapshots/{snapshot_id}/edges?limit=500
The graph itself — nodes come from `/wallets`, edges from here.
```json
[{"src_wallet_id": 1475, "dst_wallet_id": 1476, "edge_weight": 4.7485685e+17, "transfer_count": 1}]
```
`edge_weight` can be `0.0` (real data has this — a `transfer_count` of 211
with `edge_weight: 0.0` was observed) — don't treat zero as "no edge" or
divide by it. No cursor, just `limit` — pass a large `limit` (the frontend
uses `1000000` for "full graph") to get everything; there's no way to page
beyond that.

### GET /runs/{run_id}/metrics?metric_name=pagerank&limit=50
`metric_name` is whichever algorithm wrote it — open string, no fixed
enum. Louvain output comes from `/clusters`, not this endpoint.
```json
[{"wallet_id": 1647, "metric_value": 0.029165934242878663, "rank_position": 1,
  "analysis_backend": "pandas+networkx (CPU)", "library_version": null}]
```
`analysis_backend`/`library_version`: same meaning and nullability as on
`/clusters` — see there.
BFS: `metric_name=bfs_distance_from_{seed_wallet_id}` (e.g.
`bfs_distance_from_1476`); `metric_value` there is hop distance, not a
score — `0.0` at the seed, `1.0`, `2.0`, ... outward.
**Trap, confirmed against real data**: pagerank/BFS `run_id`s and Louvain
clustering `run_id`s are different numbers in the *same* `analysis_run`
id space — e.g. on the current DB, Louvain runs are `3, 6, 9, 12`,
pagerank runs are `2, 5, 8, 11`, and BFS runs are `4, 7, 10, 13`. A UI that
reuses one `run_id` across `/clusters` and `/runs/{run_id}/metrics` calls
will silently get an empty metrics list. `/runs/{run_id}/metrics` does not
validate that `run_id` exists — an unknown or mismatched id just returns
`[]`; treat that as "no data yet," not an error.

### GET /flags?run_id=&detector=&wallet_id=&limit=100
All filters optional. `detector` is one of `bfs_proximity`,
`sybil_cluster`, `pagerank_threshold`, `impurity`, `engaged_proximity`.
```json
[{"flag_id": 2055, "run_id": 18, "wallet_id": 1478, "detector": "bfs_proximity",
  "score": 70.0, "threshold": 50.0, "created_at": "2026-10-06T10:33:28.034157"}]
```
`score`/`threshold` meaning is detector-specific — see `CLAUDE.md`'s
Definitions section and `graph/detectors.py`. `threshold` is `null` for
`sybil_cluster` (categorical membership, not a numeric cutoff).

### GET /flags/{flag_id}/evidence
```json
[{"tx_hash": "0x9a05...8cbe4", "hop": 0, "block_number": 10650165, "role": "unsolicited"}]
```
Block-ordered (not grouped by hop). `role` is `"signed"` or `"unsolicited"`
— see `CLAUDE.md`. Returns `[]`, not an error, if evidence hasn't been
collected for this flag yet — **this endpoint never computes evidence on
demand**; it only reads what `graph/evidence.py` / the audit engine has
already persisted. Unknown `flag_id` also returns `[]`.

### GET /flags/{flag_id}/audit
```json
{"flag_audit_id": 1, "flag_id": 2055, "verdict": "frameable",
 "framing_cost_wei": 4365121796310332, "framing_gas": 0,
 "minimal_tx_set": ["0xc797...e08e7"], "method": "exact",
 "backend": "pandas+networkx (CPU)", "runtime_ms": 1242,
 "created_at": "2026-10-06T11:47:03.275594"}
```
`verdict`: `"frameable"` or `"robust"` — see `CLAUDE.md`'s corrected
Definitions (2026-10-06). `framing_cost_wei`/`framing_gas`/`minimal_tx_set`
are `null` for a `"robust"` verdict and for every `sybil_cluster` result
(no framing cost is computed for that detector — see `graph/audit.py`).
`method`: `"exact"`, `"greedy_upper_bound"`, or `"yes_no"`. A greedy
result is an upper bound on the true minimum cost, never claimed exact.
Errors: `404 {"detail": "flag not audited yet"}` — distinct from a 200
with `"verdict": "robust"`.

### GET /audit/summary?run_id=
`run_id` optional (omit for all runs). One row per detector among flags
that have been audited so far (not every flag that exists — see
`graph/audit.py`'s `--limit`).
```json
[{"detector": "bfs_proximity", "total_audited": 3, "frameable_count": 3,
  "framing_cost_min_wei": 1146808000000000, "framing_cost_median_wei": 2746258519285725.0,
  "framing_cost_max_wei": 4365121796310332, "frameable_share": 1.0}]
```
`framing_cost_*_wei` are `null` when `frameable_count` is `0` (no
frameable flags to compute a cost range from — see the `sybil_cluster`
row in the example above, which has `0` frameable flags out of 2 audited).

### GET /clusters/top-flagged?run_id=&snapshot_id=&limit=8
Clusters holding the most distinct flagged wallets — what Multiview's
"Top 8 flagged" opens. Ranked by **flagged wallets**, not raw flag count, so
one wallet carrying five detector flags doesn't outrank five separately
flagged wallets.
```json
[{"cluster_id": 351299, "community_label": 7, "member_count": 26594,
  "cluster_type": null, "risk_score": null, "snapshot_id": 2,
  "flagged_wallets": 26594, "flag_count": 26761,
  "frameable_count": 0, "audited_count": 0, "frameable_share": null}]
```
`limit` is capped at 8 (`422` above that) because each Multiview panel holds
its own WebGL context. `frameable_share` is `null` — not `0.0` — when
nothing in the cluster has been audited, since "no data" and "none are
frameable" are different facts.

Cluster rows and flag rows live under **different** `analysis_run` ids (one
Louvain run, one detector run), so they are related through `snapshot_id`;
matching `run_id` to `run_id` returns nothing at all. `run_id` here is
resolved to its snapshot first.

### GET /clusters/{cluster_id}/subgraph?limit=300
One cluster's members and the edges between them, for a Multiview panel.
```json
{"cluster_id": 351299, "community_label": 7, "snapshot_id": 2,
 "member_count": 26594, "truncated": true,
 "nodes": [{"wallet_id": 215093, "address": "0x09d4…", "pagerank": 0.00127,
            "verdict": "frameable", "flag_count": 4}],
 "edges": [{"src_wallet_id": 215093, "dst_wallet_id": 174669,
            "edge_weight": 1.0, "transfer_count": 3}]}
```
`verdict` is per wallet: `frameable` (any flag on it audited frameable),
`robust`, `unaudited` (flagged, not yet audited) or `unflagged`.

**Truncation is by connected expansion, not top-N.** Taking the `limit`
highest-PageRank members looks reasonable and renders as a dust cloud:
measured on cluster 351299, the top 300 came back as 76 disconnected
fragments with 22% of nodes having no edge to anything else shown. The same
budget spent expanding outward from the highest-ranked member returns one
connected component of 300. `truncated` is always reported so a partial
view is never mistaken for a whole cluster. `limit` 1–2000 (`422` outside).

### POST /jobs  ·  GET /jobs  ·  GET /jobs/{job_id}  ·  POST /jobs/{job_id}/cancel
Runs detectors or an audit pass from the UI. Body:
`{"job_type": "detectors" | "audit", "snapshot_id": 2, "seed_wallet_id": 2,
"detector": "impurity" | null, "limit": 25 | null}`
```json
{"job_id": "fd51ea4593a2", "job_type": "audit", "snapshot_id": 2, "seed_wallet_id": 2,
 "detector": "impurity", "limit": 2, "status": "running", "total": 2, "completed": 1,
 "message": "auditing flag 65206 (2/2)", "error": null,
 "result": {"verdicts": {"frameable": 1, "robust": 0}}, "elapsed_seconds": 31.4}
```
`status` is `queued | running | done | failed | cancelled`. Errors: `409` in
mock mode and when a job is already running (one at a time, by design);
`422` for an unknown `job_type`; `404` for an unknown `job_id`.

Jobs live **in memory** — a backend restart forgets them, which the `404`
detail says. What a job wrote to Postgres survives, because detectors and
audits commit as they go. Jobs only ever INSERT. `limit` samples in
`flag_id` order, the same sample `graph/audit.py --limit` takes.

Cancelling stops at the next flag; it never undoes committed work, and the
job then reports what it actually completed.

### GET /flags/{flag_id}/audit.csv
One flag's audit report plus its evidence rows as CSV, rendered server-side
so the file holds what the database says rather than what the UI had loaded.
`Content-Disposition: attachment`. `404` for an unknown flag, `409` in mock
mode.

### Authentication (Phase 5)
Every route requires a session **except** `/health`, `/benchmarks` and the
`/auth/*` routes themselves. `FRAMECHECK_AUTH_MODE=disabled` turns the gate
off for local development; anything other than exactly `disabled` (including
a typo or an unset variable) leaves it **on**, so the failure direction is
closed. Unauthenticated calls get:
```json
{"detail": "sign-in required"}
```
with status `401`. The body never says whether a token was missing, expired,
tampered with, or signed with a different secret — that distinction goes to
the server log only.

The session travels as an HttpOnly cookie (`framecheck_session`) for the
browser, or `Authorization: Bearer <token>` for the desktop app, which has
no cookie jar shared with its sidecar.

CORS is no longer a wildcard: a credentialed request may not be answered with
`Access-Control-Allow-Origin: *`, so the origin is echoed back for localhost
and the Tauri webview only.

### GET /auth/login
PKCE parameters for a client about to start the flow. `503` when the server
has no `GOOGLE_CLIENT_ID` configured.
```json
{"client_id": "…apps.googleusercontent.com", "code_verifier": "…", "code_challenge": "…",
 "code_challenge_method": "S256",
 "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
 "scope": "openid email profile"}
```
Never returns the client secret — none is used anywhere on this path.

### POST /auth/callback
Body: `{"id_token": "<Google ID token>"}`. Verifies the token against
Google's keys, upserts the user (keyed on the `sub` claim, never email), and
issues the session.
```json
{"user": {"user_id": 1, "email": "a@example.com", "display_name": "A", "picture_url": null},
 "session_token": "…"}
```
Errors: `401 {"detail": "sign-in failed"}` for any verification failure — the
reason (bad audience, unverified email, expired) is logged, not returned.

### GET /auth/session
Public, so the UI can ask before it has a session and get an answer rather
than a `401` it has to special-case.
```json
{"authenticated": false, "auth_mode": "required", "user": null}
```

### POST /auth/logout
Clears the session cookie. `{"status": "signed out"}`.

### GET /benchmarks
CPU-vs-GPU timings recorded by `scripts/benchmark.py --json`, newest value
per snapshot, ordered by graph size.
```json
[{"snapshot_id": 1, "snapshot_name": "pipeline_20260910T165758",
  "vertex_count": 19133, "edge_count": 35776, "repeats": 3,
  "recorded_at": "2026-10-08T05:57:56+00:00",
  "backends": {"cpu": "pandas+networkx (CPU)", "gpu": "cudf+cugraph (GPU)"},
  "tasks": [{"task": "pagerank", "cpu_seconds": 0.0498, "gpu_seconds": 0.0121, "speedup": 4.12}]}]
```
Reads `docs/perf/benchmarks.json`, **not** Postgres: these measure the
machine, not pipeline output, so this route answers with no database at
all. `[]` when nothing has been benchmarked yet — an empty state, not an
error. `gpu_seconds` and `speedup` are absent on a run from a machine
without RAPIDS; the UI shows "CPU only" rather than implying a 1× speedup.

### POST /internal/api-key (internal — not part of the UI contract)
Body: `{"api_key": "string"}`. Header: `X-Internal-Token: <token>`.
```json
{"status": "ok"}
```
Errors: `403 {"detail": "invalid or missing internal token"}`.
Loopback-only hand-off from the Rust shell (which owns the key in the OS
keychain) to the running Python process, once per key change. The token is
random, generated fresh by Rust each app launch, passed to the sidecar at
spawn. The UI never calls this directly; it is Rust-to-Python only,
documented here because it is a real HTTP endpoint.

**Corrected 2026-10-11:** this section used to argue the route was
unreachable from the webview because `CORSMiddleware.allow_methods` only
listed `GET`. That stopped being true when Phase 5 added `POST` for the auth
and job routes, so the CORS argument no longer holds. What actually protects
the route is the internal token — a fresh random value per launch, held by
Rust and never given to the page — plus the loopback bind. A page would have
to steal the token to use this, and nothing hands it one.

### GET /db/status
Everything the Database view's header needs, in one call.
```json
{"mode": "live",
 "settings": {"host": "localhost", "port": "5432", "dbname": "framecheck",
              "user": "postgres", "password_set": true},
 "server": {"reachable": true, "error": null, "database_exists": true,
            "server_version": "PostgreSQL 16.15 ..."},
 "schema": {"state": "tracked", "schema_applied": true, "table_count": 24,
            "migrations_available": ["002_framecheck.sql", "..."],
            "migrations_applied": ["002_framecheck.sql", "..."],
            "migrations_pending": []}}
```
`200` even when the server is unreachable — "Postgres is not running" is an
answer the setup view has to render, not an error that blanks it. `schema`
is `null` until the database exists.

`schema.state` is one of three, deliberately not collapsed:
- `empty` — no FrameCheck tables; everything can be applied in order.
- `untracked` — FrameCheck tables exist but there is no `schema_migration`
  table, so which migrations ran is genuinely unknown. `migrations_pending`
  is `null`, not `[]`: unknown is not the same as none. Every database built
  by this project before 2026-10-11 is in this state.
- `tracked` — `schema_migration` lists what has been applied.

The password is never returned, only `password_set`.

### POST /db/create
Creates the configured database if it is missing.
```json
{"created": true, "detail": "database 'framecheck' created"}
```
An existing database returns `created: false` and is left untouched — this
never drops or recreates anything. Errors: `503` (server unreachable),
`400` (Postgres refused, e.g. the user lacks CREATEDB).

### POST /db/schema
Applies `db/schema.sql` (only when absent) and then every unapplied
migration, each in its own transaction.
```json
{"applied": ["schema.sql", "002_framecheck.sql", "..."], "state": {"...": "as /db/status"}}
```
Measured on an empty database: 0.57 s for schema.sql plus all seven
migrations. Errors: `409` when `schema.state` is `untracked` — re-running a
migration that already ran would fail confusingly, so it refuses and points
at `/db/adopt`. `503` unreachable, `400` SQL error.

### POST /db/adopt
Records every migration file as applied **without running it**, for a
database that predates migration tracking.
```json
{"adopted": ["002_framecheck.sql", "..."], "state": {"...": "as /db/status"}}
```
A claim about history that cannot be verified from inside the database, so
it is a separate explicit action rather than something `/db/schema` infers.
Errors: `409` when the database has no FrameCheck tables, where the claim
would be plainly false.

### GET /db/tables
```json
{"tables": [{"table": "wallet", "rows": 610055, "rows_are_estimates": true,
             "estimate_unavailable": false, "bytes": 123456}]}
```
`rows` comes from `pg_class.reltuples`, not `count(*)`: counting every row
of all 23 tables of the real database measured **6.85 s**, versus 0.013 s
for the estimate. A table that has never been analysed reports
`rows: null, estimate_unavailable: true` rather than Postgres' raw `-1`.

### GET /db/tables/{table}/count
Exact `count(*)` for one table, for when the estimate is not enough.
```json
{"table": "wallet", "rows": 610055, "rows_are_estimates": false}
```

### GET /db/tables/{table}?limit=50
Read-only peek. `limit` is clamped to 1..500.
```json
{"table": "flag", "columns": ["flag_id", "run_id", "..."], "rows": [[1, 39, "..."]], "limit": 50}
```
The table name is checked against `information_schema.tables` and quoted as
an identifier — never interpolated. `422` for a malformed name, `404` for one
absent from this database. `NUMERIC(78,0)` wei values come back as strings;
they exceed float precision.

### GET /audit/trail
Whether the audit hash chain still verifies, and what has been removed from it.
```json
{"available": true, "rows_checked": 13, "intact": true,
 "first_bad_audit_id": null, "reason": null,
 "tombstone_count": 1,
 "tombstones": [{"flag_audit_id": 14, "flag_id": 1, "verdict": "robust",
                 "deleted_by": "postgres", "deleted_at": "2026-10-12T01:30:00+00:00",
                 "reason": "benchmark_audit.py removing its own timing rows"}]}
```
When the chain does not verify, `intact` is false and `first_bad_audit_id`
names the first row that does not fit, with `reason` one of three
distinguishable cases: the row has no hash, the row does not link to the one
before it, or the row's contents do not match its own hash. A broken chain is
not proof of foul play — a restored backup breaks it too — it means the
database can no longer vouch for those rows.

`{"available": false, "detail": "..."}` when migration 009 has not been
applied to the connected database. The chain is reported as absent rather
than intact, because a green tick for a chain that does not exist is worse
than no tick at all.

Verification runs **in the database** (`flag_audit_chain_verify()`), not in
Python: a verifier in the application could be made to agree with whatever it
was shown. Measured at 1.0 ms for 13 rows.

### GET /flags/{flag_id}/versions
Every version a flag row has had, oldest first, with the current row last.
```json
{"available": true, "flag_id": 1, "versions": [
  {"version": 1, "valid_from": "2026-10-07T02:20:44.650999+00:00",
   "valid_to": "2026-10-12T01:35:55.410055+00:00", "is_current": false,
   "change_op": "updated", "changed_by": "postgres",
   "row": {"flag_id": 1, "detector": "bfs_proximity", "score": 70, "threshold": 50}},
  {"version": 2, "...": "...", "is_current": true, "change_op": null, "changed_by": null}
]}
```
`change_op` is `updated` or `deleted` for a historical version and `null` for
the current row. 404 for a flag id that has never existed; `available: false`
when migration 009 is not applied.

### GET /flags/as-of
`?at=<timestamp>&limit=50&detector=<name>` — the flag table as it stood at an
instant, straight out of the `flag_as_of(timestamptz)` SQL function.
```json
{"available": true, "as_of": "2026-10-11T00:00:00Z", "total": 1775,
 "flags": [{"flag_id": 1, "run_id": 13, "wallet_id": 401,
            "detector": "bfs_proximity", "score": 70, "threshold": 50,
            "created_at": "2026-10-07T02:20:44.650999"}]}
```
`limit` is capped at 500. A timestamp the server cannot parse earns a 422
naming an example. One quirk is handled rather than inflicted on callers: an
ISO timestamp with a `+00:00` offset loses its `+` to URL decoding and
arrives as a space, so a space followed by an offset-shaped tail is read back
as the `+` it was — a date and time separated by a space is left alone.

Measured: 2.25 ms for 1,775 flags against 0.14 ms for a plain `count(*)` on
`flag`. The difference is the anti-join against `flag_history` that decides
which rows a later version supersedes.

### GET /db/datasets
What can be imported.
```json
{"demo": {"path": ".../datasets/snapshot1_polynetwork.sql.gz", "bytes": 18886228,
          "manifest": {"format": "framecheck-dataset v1", "snapshot_id": 1,
                       "snapshot_name": "pipeline_20260910T165758",
                       "vertex_count": 19133, "edge_count": 35776,
                       "total_rows": 443963, "row_counts": {"wallet": 25462, "...": 0},
                       "excluded_tables": ["app_user", "framing_scenario", "schema_migration"],
                       "excludes_simulated_transactions": true}}}
```
`{"demo": null}` when the build carries no dataset — a source checkout that
has never run `scripts/export_dataset.py` has no `datasets/` directory, and
the UI says so rather than offering a button that cannot work.

### POST /db/import
Body: `{"source": "demo"}` or `{"source": "file", "path": "C:\\...\\dataset.sql.gz"}`.
```json
{"manifest": {"...": "as above"},
 "loaded": {"wallet": 25462, "transaction": 96893, "...": 0},
 "skipped_seed_tables": {"analyst": 1, "algorithm": 8},
 "remapped_ids": {"analysis_run": 16},
 "total_rows": 443954, "sequences_reset": 13}
```
One transaction: a half-loaded dataset would leave foreign keys pointing at
rows that never arrived. Blocking rather than a job — the bundled dataset
measured **8.5 s** for 444k rows. Runs `ANALYZE` on the loaded tables
afterwards so `/db/tables` reports real numbers immediately instead of
`estimate_unavailable` on everything.

`skipped_seed_tables` and `remapped_ids` describe two things the import does
that are worth seeing rather than inferring:

- **Seed tables are kept as the target has them.** `db/schema.sql` seeds
  `analyst` and `algorithm`, so a database that has only had its schema
  applied is not empty, and those rows are not replaced.
- **Ids are translated by name.** Ids are not portable between FrameCheck
  databases: the database this project developed against numbers
  `pagerank_threshold` 12, while a database freshly created from
  `schema.sql` numbers it 6. An import that trusted the file's ids failed
  with `analysis_run_algorithm_id_fkey: Key (algorithm_id)=(10) is not
  present`, and merging the file's rows instead would have collided with the
  unique constraint on `algorithm_name`. So `analysis_run`, `wallet_label`
  and `alert` have their seed-referencing columns rewritten through a
  name-to-id map. A name the target does not know maps to nothing and the
  row fails its foreign key, rolling the import back — deliberately, since
  silently dropping an analysis run would make the import quietly
  incomplete.

Errors: `409` when the target already holds rows in any of the dataset's own
tables (FrameCheck will not delete them to make room) or when the file names
a table it may not write to; `422` for a file that is not a FrameCheck
dataset — refused on its header, before anything is written; `404` for a
missing file or a build with no demo dataset; `503` unreachable.

### POST /db/mode
Body: `{"mode": "live" | "mock"}`.
```json
{"mode": "live"}
```
Switches data source without a restart — an installed app has no `.env` to
edit. `live` is refused with `409` unless the server is reachable, the
database exists and the schema is applied: switching and then failing every
subsequent request would look like the app breaking.

### POST /internal/db-settings (internal — not part of the UI contract)
Body: the five DSN fields. Header: `X-Internal-Token: <token>`.
```json
{"status": "ok", "settings": {"host": "...", "password_set": true}}
```
Same loopback + per-launch-token arrangement as `/internal/api-key`, for the
same reason: the password lives in the OS keychain and reaches the backend
from Rust, never from the page. Resets the connection pool. `403` without
the token, `422` for a half-filled DSN.

### GET /ingest/plan
```json
{"api_key_set": true, "requests_per_second": 4.0,
 "source": "configured", "reported_by_etherscan": null}
```
`source` is the honest field. Etherscan only reports an allowance for paid
keys, so for most users this is `"configured"` — the rate the app is using,
never presented as a detected plan. Any non-answer from the probe
(unsupported, error status, unexpected shape, network failure) degrades to
`"configured"`.

### POST /ingest/start
Body: `{"address": null, "hops": 1, "requests_per_second": null, "max_calls": 20000}`.
Returns a job (same shape as `POST /jobs`, with `job_type: "ingest"`), whose
`result` grows as it runs:
```json
{"addresses_processed": 184, "hops_completed": 1, "frontier_size": 912,
 "calls_made": 553, "calls_remaining": 19447, "stopped_reason": null}
```
`total` stays `0` on purpose: a BFS frontier is discovered as it goes, so a
percentage would need a made-up denominator. `requests_per_second` paces the
calls; `max_calls` is a hard stop, so a multi-hour crawl cannot quietly
consume a whole day's allowance — hitting it ends the job as `done` with
`stopped_reason: "budget_exhausted"`, not as a failure. Cancel
(`POST /jobs/{id}/cancel`) takes effect after the address in flight, which is
also a commit and checkpoint boundary, so nothing already ingested is lost.
Errors: `409` in demo mode or with no key saved, `422` for `hops` outside
0..3 or `max_calls < 1`. The API key is stripped from every job response.

### Known simplifications

Two entries here described v1 and were left standing after they stopped
being true; corrected 2026-10-11.

- ~~No auth.~~ Google sign-in exists as of Phase 5 (`/auth/*`), and
  `auth.auth_mode()` fails closed: anything other than the exact string
  `disabled` in `FRAMECHECK_AUTH_MODE` means a session is required for every
  route outside `auth.PUBLIC_PATHS`. The desktop app runs with the gate off,
  since its sidecar is bound to loopback behind a per-launch internal token.
- ~~CORS is wide open (`GET` only).~~ `allow_methods` is `["GET", "POST"]`
  as of Phase 5, and the origin is an explicit regex (localhost, 127.0.0.1,
  `tauri://localhost`), not `*` — a wildcard is invalid with
  `allow_credentials=True`, which the session cookie needs.
- No pagination cursor anywhere except the `run_id`-scoped `/clusters`
  (which returns everything for that run, no cap) — everything else is
  `limit` only, with `/clusters` unfiltered capped at a non-representative
  100 rows.
- Row counts in `/db/tables` are estimates, by design — see that endpoint.

---

## Tauri commands (Rust, `src-tauri/src/commands.rs`)

Registered in `src-tauri/src/lib.rs:77-87`
(`tauri::generate_handler![...]`) — that list is the definitive set; if a
command isn't there, the webview can't call it regardless of what's in
`commands.rs`. Argument names are camelCase from JS
(`window.__TAURI__.core.invoke("run_layout", {snapshotId: ..., maxEdges: ...})`),
mapped automatically to the snake_case Rust parameter names shown below —
this is Tauri's default convention, not something this repo configures.

All commands returning `Result<T, String>` surface the error as a rejected
JS Promise with that string as the message on failure.

### get_api_base() -> String
No params. Returns `"http://127.0.0.1:{port}"` — the sidecar's actual
bound port (assigned at spawn, not fixed at 8000). Every other HTTP call
the frontend makes should be built from this, not a hardcoded port.

### get_backend_logs() -> Vec\<String\>
No params. Recent sidecar stdout/stderr lines, oldest first (for a future
"view logs" panel — a runtime backend error is otherwise invisible in a
packaged release build with no console).

### detect_gpus() -> GpuDetectionResult
No params. Native GPU adapter enumeration (DXGI, Windows-only — see
`gpu.rs`), independent of and not used by `run_layout`/`benchmark_layout`
(those use wgpu's own adapter enumeration via `create_gpu_device`, which
can select differently — see `HYBRID_MACHINE_CHECKLIST.md` for the
cross-check on hybrid hardware).
```ts
type GpuDetectionResult = {
  adapters: {
    name: string,
    vendor_id: number,       // PCI-SIG vendor id, e.g. 0x8086 = Intel
    device_id: number,
    adapter_type: "Dedicated" | "Integrated" | "Virtual" | "Unknown",
    dedicated_vram_bytes: number,
    dedicated_system_memory_bytes: number,
    shared_system_memory_bytes: number,
    reason: string | null,   // set only if classification fell back to a heuristic
  }[],
  error: string | null,      // set only if enumeration failed entirely
}
```
Real example (this machine, Intel Arc integrated, no dedicated adapter):
```json
{"adapters": [{"name": "Intel(R) Arc(TM) Graphics", "vendor_id": 32902,
  "device_id": 32085, "adapter_type": "Integrated",
  "dedicated_vram_bytes": 8589934592, "dedicated_system_memory_bytes": 0,
  "shared_system_memory_bytes": 9409404887, "reason": null}], "error": null}
```

### has_api_key() -> bool
No params. Deliberately a bool, not the key — Settings never reads the
stored value back to render it (write-only by design).

### set_api_key(newKey: string) -> Result\<null, String\>
Errors (as rejected promise message): `"key cannot be empty -- use clear_api_key to remove it"`,
or a keychain-layer error, or `"backend rejected the key update: HTTP {status}"` /
`"could not reach backend to apply the new key: {err}"` if the sidecar
hand-off fails.

### clear_api_key() -> Result\<null, String\>
No params beyond the injected state. Same hand-off error shapes as `set_api_key`.

### test_api_key(candidateKey?: string) -> Result\<TestResult, String\>
```ts
type TestResult = { ok: boolean, message: string }
```
Tests `candidateKey` if given (unsaved value while typing), else whatever's
in the keychain. `ok: false` cases are returned as `Ok(TestResult)`, not
`Err` — only a missing-key-to-test-at-all case (`Err("no key saved yet, and
none provided to test")`) is a rejected promise. Real message strings the
UI should expect verbatim: `"No network — the request timed out."`,
`"No network — could not reach Etherscan."`, `"Rate limited by Etherscan —
try again in a moment."`, `"Connected — key is valid."`, `"Invalid API key."`,
or `"Unexpected response from Etherscan: {message}"`.

### set_db_settings(settings) -> Result\<null, String\>
`settings` is `{host, port, dbname, user, password}`. Saves them to the OS
keychain and pushes them to the running backend over the loopback internal
route. **An empty `password` means "keep the saved one"** — the page cannot
read the stored password back, so a blank field has to mean no change rather
than "set it to nothing". Validated before anything is written, so a
half-filled form does not put a password in the keychain.

### has_db_settings() -> bool
Like `has_api_key`: a bool, never the value.

### run_layout(snapshotId, maxEdges, device, runId, clusterAttractionK) -> Result\<LayoutResult, String\>
| param | type | meaning |
|---|---|---|
| `snapshotId` | number | which `graph_snapshot` to lay out |
| `maxEdges` | number | edge fetch cap (frontend uses up to `1000000` for "full graph") |
| `device` | string | `"gpu"` (any adapter), `"gpu-discrete"` / `"gpu-integrated"` (specific type, for a hybrid machine's device picker), anything else (including `"cpu"`) = CPU rayon |
| `runId` | number | which clustering `run_id` to pull cluster membership from — **only used if `clusterAttractionK > 0`** |
| `clusterAttractionK` | number | `<= 0` disables cluster-aware forces entirely (no membership fetch at all); `>0` pulls each node toward its cluster's live centroid every iteration, strength `k` |

```ts
type LayoutResult = {
  positions: Record<string, [number, number]>,  // wallet_id (as string key) -> [x, y]
  device_requested: string,   // echoes the `device` param verbatim
  device_used: string,        // what actually ran — see fallback strings below
  node_count: number,
  edge_count: number,
  iterations: number,
  elapsed_ms: number,
  converged: boolean,
}
```
`device_used` real values observed: `"gpu: {adapter name} ({adapter type})"`
e.g. `"gpu: Intel(R) Arc(TM) Graphics (IntegratedGpu)"`, `"cpu"`, or
`"cpu (no matching GPU adapter found, fell back)"` when `device` requested
GPU but none was found. `converged` is `false` in every real measurement
against the case-study graph — the settle loop hits its 500-iteration/20s
cap before `CONVERGENCE_EPS` is satisfied at this density on any device;
this is documented as expected, permanent behavior, not a bug.
Errors: `Err("snapshot {id} has no edges to lay out")` if the snapshot is
empty; edge-fetch/malformed-response errors from the sidecar call surface
as-is.

**Side effect**: emits a Tauri event `"layout-progress"` roughly every 20
iterations while settling, same payload shape as `positions` above
(`Record<string, [number, number]>`) — partial, not-yet-final positions,
for a UI to show the layout visibly settling instead of only appearing
once the whole command resolves. Listen via
`window.__TAURI__.event.listen("layout-progress", event => {...})`;
`event.payload` is the position map. Not guaranteed to fire at all for a
very small/fast graph that converges before the first 20-iteration
checkpoint.

### benchmark_layout(snapshotId, maxEdges) -> Result\<BenchmarkResult, String\>
Runs CPU single-thread, CPU rayon, and GPU (if any adapter found) back to
back on the same real snapshot graph, for direct comparison / reproducing
on other hardware.
```ts
type BenchmarkResult = {
  node_count: number,
  edge_count: number,
  rows: {
    label: string,            // "CPU single-thread" | "CPU rayon ({N}T)" | "GPU ({adapter name})"
    per_iter_ms: number,
    settle_ms: number,
    settle_iterations: number,
    converged: boolean,
    readback_ms: number | null,  // GPU only; null for CPU rows
  }[],
}
```
`rows` has 2 entries (CPU only) if no GPU adapter is found, 3 otherwise.
Errors: same "no edges" case as `run_layout`.

---

## Not yet implemented (planned, not to be assumed present)
- Device list/set/active-device commands, and an "auto-recommend device for
  N nodes" command — see `docs/UI_HANDOFF.md` for what's pending on this.
- Connected components, label propagation — not approved, not started.
