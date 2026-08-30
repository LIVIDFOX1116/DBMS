# Benchmark: polynetwork_1hop_v1

Subgraph: 19119 vertices, 35756 edges. BFS seed wallet_id=1476. Each stage timed 5 time(s); median and range reported below.

| Stage | pandas+networkx (CPU) |
|---|---|
| load_edgelist | 158.5 ms median (range 103.6-166.3 ms, n=5) |
| pagerank | 61.3 ms median (range 60.7-264.7 ms, n=5) |
| louvain | 2457.9 ms median (range 2269.6-2486.5 ms, n=5) |
| bfs | 0.4 ms median (range 0.4-1.1 ms, n=5) |

Median is the steady-state figure to compare across backends; the range shows how much the first in-process call can be inflated by one-time library-import / OS disk-cache costs rather than the algorithm itself.

## Result agreement (sanity check)

- **pandas+networkx (CPU)**: top PageRank wallet_id=1647, 9531 Louvain communities (modularity 0.7259), BFS reached 76 wallets