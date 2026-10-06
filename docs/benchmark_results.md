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

# Benchmark: live_full (real Etherscan crawl, snapshot_id=2)

Subgraph: 253,863 vertices, 355,989 edges -- 13x the polynetwork_1hop_v1 graph above, and the first GPU-vs-CPU comparison ever run against real (not synthetic) data at this scale. BFS seed wallet_id=2 (the case-study address itself). One full `graph.run_analysis` invocation per backend (not the 5-repeat in-process median/range methodology above) -- see the caveat below before comparing rows across the two tables.

| Stage | cudf+cugraph (GPU, GTX 1650) | pandas+networkx (CPU) | GPU vs CPU |
|---|---|---|---|
| pagerank | 43.6s (avg of 2 runs: 43.2s, 44.0s) | 43.5s | ~no difference |
| louvain | 74.5s (avg of 2 runs: 75.3s, 73.7s) | 103.3s | GPU ~1.27x faster |
| bfs | 6.2s (avg of 2 runs: 6.3s, 6.2s) | 5.4s | CPU slightly faster |

**Methodology caveat, and why this looks different from the table above:** these numbers are `analysis_run.runtime_ms` from real end-to-end `graph.run_analysis` calls, which time the *whole* stage -- including `persist_vertex_metric`/`persist_clusters` writing one row per wallet back to Postgres via individual, unbatched `INSERT ... ON CONFLICT` calls (253,863 of them for pagerank alone). That write loop costs the same regardless of which backend computed the values, so at this scale it very plausibly dominates wall-clock time and compresses the apparent GPU/CPU gap -- this is *not* pure algorithm-compute timing like the polynetwork_1hop_v1 table above, which measured `backend.pagerank(G)` etc. in isolation, repeated 5x, with the one-time import/cache costs called out separately. A fairer GPU-vs-CPU comparison would isolate algorithm time from persistence time (e.g. batch the writes via `execute_values`/`COPY`, or time `backend.pagerank(G)` directly the way the older benchmark did) -- not done here since it wasn't the ask, but worth knowing before citing these numbers as "the GPU speedup."

**Don't conflate this with the desktop app's force-layout GPU benchmarks** (`gpu-bench/`, 5x-149x GPU speedup at >=10k nodes): that's a different engine (`src-tauri/src/layout.rs`, wgpu compute shaders) doing a different, far more parallelizable workload (naive O(n^2) all-pairs repulsion, embarrassingly parallel across GPU cores) for a completely different purpose (force-directed layout positions for rendering, not graph analytics). The two GPU paths in this project are architecturally separate and their benchmark numbers aren't comparable to each other.

**Real, honest takeaway:** on a GTX 1650 (entry-level, 4GB VRAM) at ~254k-vertex/~356k-edge scale, `cugraph`'s GPU advantage over `networkx` is modest-to-none for pagerank/bfs and only clearly real for louvain -- likely because Postgres persistence overhead (backend-independent) and/or CUDA dispatch/transfer fixed costs dominate at this graph size on this GPU, not because cugraph itself is slow. This does not mean "GPU acceleration doesn't work here" -- `gpu_device` correctly shows `cudf+cugraph (GPU)` on every GPU run, confirming the path genuinely executes on-GPU, not a silent CPU fallback -- it means the *speedup from doing so* is smaller than the project's earlier layout-engine benchmarks might lead you to assume, at this specific scale, on this specific card, with this specific (unbatched-write) pipeline.