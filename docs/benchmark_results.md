# Benchmark: CPU vs GPU

Re-measured 2026-10-11 (Phase 11), after the Phase 10 database work. Both
passes run in **one process inside WSL2**, so the only thing that differs
between the columns is the backend. Each stage is timed 3 times and the
**median** reported. See `docs/GPU_SETUP.md` for the environment.

| | |
|---|---|
| CPU backend | pandas + NetworkX |
| GPU backend | cudf + cuGraph 26.08 |
| GPU | NVIDIA GeForce GTX 1650, 4 GB (Turing) |
| Host | WSL2 Ubuntu-24.04, Python 3.12.3 |

The GPU here is a 4 GB laptop part, not the RTX 4070 the brief named. Read
these as what this hardware does, not as what cuGraph can do.

## Did the GPU actually run, or did everything fall back to CPU?

**It ran.** The brief asked this directly, so it was verified three
independent ways rather than trusting the recorded backend string:

| Signal | Result |
|---|---|
| `gpu_available()` | `True`, with `FRAMECHECK_USE_GPU_GRAPH=1` |
| Backend reported | `cudf+cugraph (GPU)`, cuGraph 26.08.00 |
| **Graph object type** | `cugraph.structure.graph_classes.Graph` — a fallback would be `networkx.DiGraph`, which is exactly what the CPU path returns |
| **Device memory** | 838 MB in use before building the graph, 876 MB after — real allocation on the device |

All three snapshots record `backends.gpu` in `docs/perf/benchmarks.json`.

## snapshot 1 — `pipeline_20260910T165758` (19,133 vertices, 35,776 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 0.0869 | 0.0211 | 4.12× |
| pagerank | 0.0365 | 0.0118 | 3.09× |
| louvain | 1.4228 | 0.1552 | 9.17× |
| bfs | 0.0003 | 0.0162 | **0.02× (GPU 50× slower)** |

## snapshot 2 — `live_full_20260924T214007` (253,863 vertices, 355,989 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 1.2554 | 0.0602 | 20.85× |
| pagerank | 0.5152 | 0.0419 | 12.30× |
| louvain | 25.4504 | 0.7825 | 32.52× |
| bfs | 0.0646 | 0.0375 | 1.72× |

## snapshot 3 — `live_full_20260925T095654` (455,073 vertices, 698,273 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 2.6754 | 0.0603 | 44.37× |
| pagerank | 1.0008 | 0.1033 | 9.69× |
| louvain | 52.4882 | 1.0886 | 48.22× |
| bfs | 0.1074 | 0.0390 | 2.75× |

## Audit pass (`pagerank_threshold`, snapshot 2, 5 flags sampled)

| Task | CPU | GPU | Speedup |
|---|---|---|---|
| audit_flag | 14.02 s/flag (4.3 flags/min) | 0.76 s/flag (78.9 flags/min) | **18.4×** |

Only `pagerank_threshold` has a GPU path — its framing-cost search re-runs
masked PageRank per candidate. The other four detectors' audits are CPU
replay by design. Each pass deleted exactly the `flag_audit` rows it created.

## Where the GPU starts winning

Peak speedup is a misleading headline on its own: the same hardware that runs
Louvain 48× faster runs BFS **50× slower** on a small graph. `scripts/
benchmark_crossover.py` walks real edge samples from snapshot 3 to find where
each algorithm actually crosses over.

| Edges | load | pagerank | louvain | bfs |
|---|---|---|---|---|
| 2,000 | 0.77× | 1.21× | 1.98× | **0.02×** |
| 5,000 | 0.61× | 0.57× | 5.66× | **0.01×** |
| 10,000 | 1.03× | 1.20× | 8.97× | **0.02×** |
| 20,000 | 3.50× | 5.56× | 17.88× | **0.03×** |
| 40,000 | 5.06× | 5.13× | 10.57× | 1.70× |
| 80,000 | 19.11× | 15.57× | 10.56× | 1.96× |
| 160,000 | 14.48× | 9.18× | 12.13× | 4.42× |
| 320,000 | 21.57× | 14.55× | 22.28× | 4.13× |
| 640,000 | 44.30× | 14.80× | 48.90× | 2.87× |

| Algorithm | Crossover |
|---|---|
| `pagerank` | ahead from the smallest size measured (2,000 edges) |
| `louvain` | ahead from the smallest size measured (2,000 edges) |
| `load_edgelist` | between **5,000 and 10,000** edges |
| `bfs` | between **20,000 and 40,000** edges (0.03× → 1.70×) |

**BFS is the honest outlier.** At 20,000 edges the GPU is still 33× slower.
Two things cause it: fixed host-to-device transfer and kernel-launch cost
that a sub-millisecond traversal cannot amortise, and a genuine difference in
work done — the CPU BFS stops once it has reached the wallets it can reach
(76 on snapshot 1), while `cugraph.bfs` returns a row for **every** vertex in
the graph. The GPU is not doing the same job slower; it is doing a bigger job.

Louvain is the opposite and the one that changes what is practical: 52.5 s on
CPU at 698k edges against 1.09 s on GPU.

## Did the Phase 10 database work change these numbers?

**No, and it was not expected to** — `scripts/benchmark.py` reads the snapshot
Parquet files, not Postgres, so the indexes and materialized view added in
Phase 10 are not on this path. Comparing Phase 4 against Phase 11:

| Snapshot | Task | Phase 4 | Phase 11 |
|---|---|---|---|
| 2 | louvain | 33.68× | 32.52× |
| 2 | pagerank | 11.82× | 12.30× |
| 3 | louvain | 44.75× | 48.22× |
| 3 | bfs | 2.09× | 2.75× |

Differences are run-to-run variation (±20% on the small tasks), not an effect
of the database change. Reported as a non-result rather than dressed up.

## Still unresolved: the two backends do not agree on PageRank

This remains open from Phase 4 and is **not** fixed by anything here. On
snapshot 3 the CPU and GPU produce materially different PageRank scores:
disjoint top-5 wallets, 84/100 top-100 overlap, and GPU top scores ~3.5×
larger — while overall rank correlation stays at 0.9995.

Ruled out by measurement: the GPU is not dropping edge weights (an unweighted
CPU run matches it *less*, 35/100 overlap), and it is not float32 precision
(cuGraph keeps `edge_weight` float64 end to end). What remains is the weights
themselves — `edge_weight` is wei, and **71.3% of edges carry weight 0** on
that snapshot, so the two libraries' differing dangling-vertex conventions
dominate.

**Why this matters more than the speedups:** `pagerank_threshold` flags
wallets whose score crosses an *absolute* threshold, so whether RAPIDS happens
to be installed can change which wallets get flagged. Every speedup above is a
timing comparison; it is **not** a claim that both backends computed the same
answer. `graph/backend.py`'s own docstring says GPU availability must never
silently change what a run computed — this is a concrete case where it does,
and it needs a deliberate decision (run PageRank unweighted, compress the
weight range with `log1p`, or pin PageRank to one backend).

## Reproducing

```bash
# in WSL2, after docs/GPU_SETUP.md
cd /mnt/e/DBMS
export PYTHONPATH=/mnt/e/DBMS FRAMECHECK_USE_GPU_GRAPH=1
~/rapids-venv/bin/python scripts/benchmark.py --snapshot-id 2 --seed-wallet-id 2 \
    --repeats 3 --json docs/perf/benchmarks.json
~/rapids-venv/bin/python scripts/benchmark_crossover.py --snapshot-id 3 --repeats 3

# audit pass: run twice, once without FRAMECHECK_USE_GPU_GRAPH and once with it
~/rapids-venv/bin/python scripts/benchmark_audit.py --snapshot-id 2 --seed-wallet-id 2 \
    --detector pagerank_threshold --limit 5
```

`docs/perf/benchmarks.json` feeds `GET /benchmarks` and the dashboard's
Benchmarks tab, which shows the crossover table above the per-snapshot
timings. A machine without RAPIDS still runs all of this and records the CPU
column alone; the tab shows "CPU only" rather than implying a 1× speedup.
