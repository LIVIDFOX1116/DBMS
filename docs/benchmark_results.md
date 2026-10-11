# Benchmark: CPU vs GPU

Re-measured 2026-10-11, after the PageRank convergence fix described below
(which changed the CPU numbers, so every table here is from that re-run; the
Phase 11 figures it replaced are kept only in the comparison table near the
end). Both passes run in **one process inside WSL2**, so the only thing that
differs between the columns is the backend. Each stage is timed 3 times and
the **median** reported. See `docs/GPU_SETUP.md` for the environment.

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
| load_edgelist | 0.0932 | 0.0149 | 6.26× |
| pagerank | 0.0504 | 0.0226 | 2.23× |
| louvain | 1.6630 | 0.2053 | 8.10× |
| bfs | 0.0065 | 0.0334 | **0.19× (GPU 5× slower)** |

## snapshot 2 — `live_full_20260924T214007` (253,863 vertices, 355,989 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 1.3939 | 0.0552 | 25.25× |
| pagerank | 0.7009 | 0.0690 | 10.16× |
| louvain | 30.1276 | 0.8771 | 34.35× |
| bfs | 0.0841 | 0.0484 | 1.74× |

## snapshot 3 — `live_full_20260925T095654` (455,073 vertices, 698,273 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 2.8659 | 0.0753 | 38.06× |
| pagerank | 1.4955 | 0.2233 | 6.70× |
| louvain | 60.9886 | 1.2259 | 49.75× |
| bfs | 0.1308 | 0.0553 | 2.37× |

## Audit pass (`pagerank_threshold`, snapshot 2, 5 flags sampled)

| Task | CPU | GPU | Speedup |
|---|---|---|---|
| audit_flag | 14.02 s/flag (4.3 flags/min) | 0.76 s/flag (78.9 flags/min) | **18.4×** |

> Measured **before** the PageRank convergence fix below, and not yet
> re-measured. The CPU side of this figure ran PageRank probes that stopped
> early, so the real CPU cost is somewhat higher and this 18.4× is a
> **lower** bound on the gap, not an overstatement. It is left here as the
> last honestly-measured number rather than quietly rescaled.

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

## Resolved: why the two backends disagreed on PageRank

Open from Phase 4 to Phase 11, and recorded in all of them as a weighting
problem. It was not. **NetworkX was never converging.**

NetworkX stops when `sum|x - x_last| < N * tol`, where `N` is the vertex
count. Its default `tol=1e-6` therefore permits a total L1 error of
**0.455** on snapshot 3's 455,073 vertices — on a distribution that sums to
1. The CPU path was quitting several iterations early, and the top of the
distribution is exactly where those iterations still matter:

| snapshot 3, wei weights | Top PageRank score |
|---|---|
| CPU, default `tol=1e-6` | 0.00097 |
| CPU, converged | 0.00349 |
| GPU | 0.00349 |

That 3.6× gap is the "GPU top scores ~3.5× larger" previously attributed to
dangling-vertex conventions and to 71.3% of edges carrying weight 0. The
weights were never the problem — the earlier evidence (an unweighted CPU run
matching *less*) was itself measured with the unconverged CPU path.

### What it cost in flags, not in scores

`scripts/pagerank_agreement.py` (new) measures the disagreement the only way
that matters: how many wallets `pagerank_threshold` actually flags, selected
through `select_pagerank_threshold_flags` — the production path, not a
reimplementation.

| Snapshot | Flags | Agreed before | Agreed after |
|---|---|---|---|
| 1 (35,776 edges) | 192 | 189 (Jaccard 0.969) | **192 (1.0000)** |
| 2 (355,989 edges) | 2,539 | 2,290 (Jaccard 0.821) | **2,539 (1.0000)** |
| 3 (698,273 edges) | 4,551 | 3,053 (Jaccard 0.505) | **4,551 (1.0000)** |

After the fix, top-100 overlap is 100/100 and Spearman 1.0000 on both
snapshots, under raw-wei, unweighted *and* `log1p` weights — so the three-way
weighting decision this section used to call for is moot. It was a
convergence bug, not a modelling choice.

Nor was it ties at the percentile cut: exactly one wallet sits on the cut
score, and none of the disagreeing wallets were within 1e-9 of it.

### The fix, and what it cost

`graph/backend.py` now derives the CPU tolerance from an absolute L1 budget,
`tol = PAGERANK_L1_TARGET / N`, which makes the criterion independent of
graph size — the actual root cause — with `max_iter=1000`. Non-convergence
is logged loudly and falls back rather than killing a pipeline run, because
an unconverged score that looks converged is the bug being fixed here. The
GPU tolerance is pinned at `1e-8` instead of inheriting cuGraph's default
(which already converged on these graphs, but was not ours to rely on).

Converging is cheap: 75 power iterations on the largest snapshot, and CPU
PageRank goes from 0.88 s to 1.03 s on snapshot 3 in isolation. The honest
consequence is that the published PageRank speedups fall, because the CPU
column now does the whole job and the GPU is held to a tighter tolerance:

| Snapshot | PageRank, before | PageRank, after |
|---|---|---|
| 1 | 3.09× | 2.23× |
| 2 | 12.30× | 10.16× |
| 3 | 9.69× | 6.70× |

`scripts/benchmark.py`'s own result-agreement check now reports the **same
top PageRank wallet on both backends** for all three snapshots (173, 1868,
1868), where the two used to pick different wallets entirely.

### What is still wrong in the database

The fix changes what the detector computes, so the flags already stored in
Postgres are the unconverged ones. Verified directly for snapshot 2, whose
latest stored PageRank run is the CPU one (`analysis_run` 10):

- stored flags vs the unconverged recomputation: **2,539 of 2,539 identical**
- stored flags vs the converged recomputation: **2,290 shared, 249 wrong in
  each direction** (9.8%)

So 249 wallets are flagged in the live database that should not be, and 249
that should be are not. Snapshot 1's stored run came from the GPU and is
unaffected; snapshot 3 has no stored PageRank run at all. Re-running the
detectors would correct this and change stored results, which is a deliberate
data change and has not been done.

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
