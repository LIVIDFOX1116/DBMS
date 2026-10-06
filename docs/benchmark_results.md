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
