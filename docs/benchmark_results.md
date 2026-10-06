# Benchmark: pipeline_20260830T125156

Subgraph: 19119 vertices, 35756 edges. BFS seed wallet_id=1476. Each stage timed 5 time(s); median and range reported below.

| Stage | pandas+networkx (CPU) |
|---|---|
| load_edgelist | 137.4 ms median (range 99.0-163.6 ms, n=5) |
| pagerank | 48.0 ms median (range 43.8-372.5 ms, n=5) |
| louvain | 1929.9 ms median (range 1858.1-2053.6 ms, n=5) |
| bfs | 0.3 ms median (range 0.3-1.8 ms, n=5) |

Median is the steady-state figure to compare across backends; the range shows how much the first in-process call can be inflated by one-time library-import / OS disk-cache costs rather than the algorithm itself.

## Result agreement (sanity check)

- **pandas+networkx (CPU)**: top PageRank wallet_id=1647, 9531 Louvain communities (modularity 0.7259), BFS reached 76 wallets

## Persistence time (Phase 2 bulk writes)

Separate from algorithm time above -- this is graph/run_analysis.py's execute_values-based DB writes, timed directly against the real database. Run inside a transaction that is always rolled back, never committed, so this leaves zero rows in the real DB.

| Stage | Time |
|---|---|
| persist_vertex_metric | 1076.4 ms median (range 850.8-1878.3 ms, n=5) |
| persist_clusters | 1833.6 ms median (range 1683.8-1858.8 ms, n=5) |

## Audit benchmark: flags audited per minute (FrameCheck Phase 9)

`scripts/benchmark_audit.py`, run against the full-extent snapshot
(snapshot_id=4, 30,322 vertices) on this CPU-only machine (no RAPIDS). GPU
numbers for `pagerank_threshold` (the one detector whose audit routes
through `GraphBackend.pagerank_excluding_signed_edges`, which is GPU-
accelerated when RAPIDS + `FRAMECHECK_USE_GPU_GRAPH=1` are available) still
need a RAPIDS-capable machine to fill in -- re-run the script unmodified
there.

| Detector | Backend | n | median s/flag | flags/min |
|---|---|---|---|---|
| sybil_cluster | CPU | 3 | 2.48 | 24.2 |
| pagerank_threshold | CPU | 1 | 3.19 | 18.8 |

**Caveat on the `pagerank_threshold` row**: `n=1` here, and that one
sampled flag happened to audit quickly -- consistent with a `robust`
verdict (one masked-PageRank re-run, no further cost-search needed), not
a `frameable` one (which loops a masked-PageRank re-run per candidate
sender added back during the cheapest-first search, and can take
substantially longer). A single sample does not characterize the real
distribution; treat 18.8 flags/min as optimistic, not representative,
until a larger run with a mix of verdicts fills this in. `bfs_proximity`,
`impurity`, and `engaged_proximity` are not yet benchmarked here (their
audits are fast, real-Postgres-dependent CPU replay with no separate GPU
path to compare against -- see `graph/audit.py`'s module docstring).
