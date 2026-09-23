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

---

## HTTP endpoints (Python sidecar, `api/main.py`)

Base URL (local dev): `http://localhost:8000` (or whatever port the Tauri
shell assigns the sidecar at spawn — see `get_api_base` below).

Run it standalone: `uvicorn api.main:app --reload --port 8000`. Interactive
OpenAPI docs are free from FastAPI at `http://localhost:8000/docs`.

Two modes, **identical shapes** in both, switched by one env var:
`TXGRAPH_API_MODE=mock|live` (default `mock`). Mock serves
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

### POST /internal/api-key (internal — not part of the UI contract)
Body: `{"api_key": "string"}`. Header: `X-Internal-Token: <token>`.
```json
{"status": "ok"}
```
Errors: `403 {"detail": "invalid or missing internal token"}`.
Loopback-only hand-off from the Rust shell (which owns the key in the OS
keychain) to the running Python process, once per key change. The token is
random, generated fresh by Rust each app launch, passed to the sidecar at
spawn. Not reachable from the webview: `CORSMiddleware.allow_methods` only
lists `GET`, so even a compromised page loaded in the app can't `fetch()`
this route — only Rust's direct (non-CORS-subject) HTTP calls can. The UI
branch never calls this directly; it's Rust-to-Python only, documented
here for completeness since it is a real HTTP endpoint.

### Known simplifications (intentional, unchanged from v1)
- No auth. Fine for a local, single-machine app; never expose this
  publicly as-is.
- No pagination cursor anywhere except the `run_id`-scoped `/clusters`
  (which returns everything for that run, no cap) — everything else is
  `limit` only, with `/clusters` unfiltered capped at a non-representative
  100 rows.
- CORS is wide open (`GET` only) — see above; safe only because nothing
  here mutates state except the token-gated internal route.

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
