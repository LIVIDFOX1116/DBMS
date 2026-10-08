# Benchmark: CPU vs GPU

Measured 2026-10-08. Both passes run in **one process inside WSL2**, so the only thing that
differs between the columns is the backend — timing CPU on Windows against GPU in WSL would
compare two Pythons and two filesystems, not two backends. Each stage is timed 3 times and the
**median** is reported; see `docs/GPU_SETUP.md` for the environment.

| | |
|---|---|
| CPU backend | pandas + NetworkX |
| GPU backend | cudf + cuGraph 26.08 |
| GPU | NVIDIA GeForce GTX 1650, 4 GB (Turing) |
| Host | WSL2 Ubuntu-24.04, Python 3.12.3 |

The GPU here is a 4 GB laptop part, not the RTX 4070 the phase brief named. Read these as what
this hardware does, not as what cuGraph can do.

## snapshot 1 — `pipeline_20260910T165758` (19,133 vertices, 35,776 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 0.0832 | 0.0129 | 6.45× |
| pagerank | 0.0377 | 0.0109 | 3.46× |
| louvain | 1.4398 | 0.1258 | 11.45× |
| bfs | 0.0004 | 0.0101 | **0.04× (GPU slower)** |

## snapshot 2 — `live_full_20260924T214007` (253,863 vertices, 355,989 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 1.1697 | 0.0406 | 28.81× |
| pagerank | 0.5131 | 0.0434 | 11.82× |
| louvain | 24.6376 | 0.7316 | 33.68× |
| bfs | 0.0618 | 0.0321 | 1.93× |

## snapshot 3 — `live_full_20260925T095654` (455,073 vertices, 698,273 edges)

| Task | CPU s | GPU s | Speedup |
|---|---|---|---|
| load_edgelist | 2.4987 | 0.0629 | 39.72× |
| pagerank | 1.0157 | 0.1015 | 10.01× |
| louvain | 50.0444 | 1.1183 | 44.75× |
| bfs | 0.1083 | 0.0517 | 2.09× |

## Audit pass (`pagerank_threshold`, snapshot 2, 5 flags sampled)

| Task | CPU | GPU | Speedup |
|---|---|---|---|
| audit_flag | 13.49 s/flag (4.4 flags/min) | 0.72 s/flag (83.7 flags/min) | 18.74× |

Only `pagerank_threshold` has a GPU path — its framing-cost search re-runs masked PageRank per
candidate (`GraphBackend.pagerank_excluding_signed_edges`). The other four detectors' audits are
CPU replay by design, so they are unaffected by the backend. Each pass deleted exactly the
`flag_audit` rows it created, by captured id range, so the database is left as found.

## Reading these numbers

- **Louvain is the real win** and it scales with the graph: 11× → 34× → 45×. It is also the only
  stage where the CPU path becomes genuinely painful (50 s on snapshot 3), so this is the speedup
  that changes what is practical.
- **BFS on the small snapshot is 25× _slower_ on the GPU.** That is not noise. The CPU BFS from
  the seed stops after reaching 76 wallets, while `cugraph.bfs` returns a row for every vertex in
  the graph; add fixed host-to-device transfer and setup cost and the GPU loses outright on a
  traversal this small. It only pulls ahead (~2×) once the traversal is large.
- **The audit speedup (18.7×) is larger than the raw PageRank speedup (10-12×)** because an audit
  runs many masked PageRanks back to back, so the one-time graph-build cost is amortised.

## PageRank does not agree between backends — known issue

The two backends produce **materially different PageRank scores on the same graph**, and this is
not a rounding difference:

| Snapshot | Top wallet agrees? | Top-100 overlap |
|---|---|---|
| 1 (35,776 edges) | yes (173) | — |
| 2 (355,989 edges) | yes (1868) | — |
| 3 (698,273 edges) | **no** — CPU says 1064, GPU says 1868 | **84 / 100** |

On snapshot 3 the overall ordering still correlates almost perfectly (Spearman **0.9995**), but
the top of the distribution differs and the GPU's top scores are ~3.5× larger (0.003485 vs
0.000971). Two hypotheses were tested and **ruled out**: the GPU is not silently dropping the edge
weights (an unweighted CPU run matches the GPU even less — 35/100 overlap), and it is not a
float32 precision loss (cuGraph keeps `edge_weight` as float64 end to end).

What is left is the weights themselves: `edge_weight` is **wei transferred**, which on snapshot 3
ranges from 0 to 3.0×10²³ — and **71.3% of edges have weight exactly 0**. A weighted PageRank over
a graph where most edges carry zero weight is ill-posed: a vertex whose out-edges all weigh 0 has
no outgoing transition probability, and NetworkX and cuGraph resolve that dangling case by
different conventions. The high rank correlation is consistent with this — the ordering is driven
by the minority of edges that actually moved value.

**Why this matters beyond the benchmark:** `pagerank_threshold` flags wallets whose score crosses
an *absolute* threshold, and `graph/audit.py` compares a masked score against that same absolute
threshold. So on a graph like snapshot 3, whether RAPIDS happens to be installed can change which
wallets get flagged and which flags come back frameable. `graph/backend.py`'s own module docstring
is explicit that GPU availability must never silently change what a run computed; this is a
concrete case where it does. **Not fixed here** — the sensible fixes (run PageRank unweighted, or
compress the weight range with `log1p`, or keep the weights and pin PageRank to one backend) each
change what every existing analysis run means, which is a decision to take deliberately rather
than inside a benchmarking phase.

Until it is settled, treat GPU PageRank as a performance comparison only, and keep producing
flags with one backend consistently.

## Reproducing

```bash
# in WSL2, after docs/GPU_SETUP.md
cd /mnt/e/DBMS
export PYTHONPATH=/mnt/e/DBMS FRAMECHECK_USE_GPU_GRAPH=1
~/rapids-venv/bin/python scripts/benchmark.py --snapshot-id 2 --seed-wallet-id 2 \
    --repeats 3 --json docs/perf/benchmarks.json

# audit pass: run twice, once without FRAMECHECK_USE_GPU_GRAPH and once with it
~/rapids-venv/bin/python scripts/benchmark_audit.py --snapshot-id 2 --seed-wallet-id 2 \
    --detector pagerank_threshold --limit 5
```

`--json` feeds `docs/perf/benchmarks.json`, which `GET /benchmarks` serves to the dashboard's
Benchmarks tab. A machine without RAPIDS still runs this and records the CPU column alone; the
tab shows "CPU only" rather than implying a 1× speedup.
