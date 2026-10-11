# Database after Phase 10

Same machine, same workload, same measurement method as
`docs/perf/db_baseline.md`. Every change below was made because a measurement
there showed a specific cost — nothing was added speculatively.

## Endpoint timings, before and after

| Endpoint | Before | After | Change |
|---|---|---|---|
| `/clusters` (unfiltered) | 653 ms | **33 ms** | 20× faster |
| `/wallets?limit=200` | 70 ms | **7 ms** | 10× faster |
| `/clusters/top-flagged?limit=8` | 528 ms | **114 ms** | 4.6× faster |
| `/clusters/351299/subgraph?limit=150` | 979 ms | **199 ms** | 4.9× faster |
| `/clusters/351299/subgraph?limit=600` | 1,034 ms | **194 ms** | 5.3× faster |
| `/clusters/354282/subgraph?limit=150` | 92 ms | **27 ms** | 3.4× faster |
| `/stats/overview` | 80 ms | 73 ms | unchanged |
| `/flags/{id}/evidence-edges` | 21 ms | 17 ms | unchanged |

Query-level, from `EXPLAIN (ANALYZE, BUFFERS)`:

| Query | Before | After |
|---|---|---|
| Q2 `/clusters` ordering | 1,157.7 ms | **0.091 ms** |
| Q4 `/wallets` top-N | 70.2 ms | **0.649 ms** |
| Q1 subgraph ranking pass | 488.8 ms (part of 1,037.9) | see below |
| Q3 top-flagged ranking | 489.8 ms | ~41 ms via the view |

## What changed

### Migration 007 — three indexes and four CHECK constraints

- `idx_cluster_run_id` on `cluster (run_id DESC)` — removes the 516,622-row
  Seq Scan and the Materialize that was re-read 17 times.
- `idx_cluster_member_wallet` on `cluster_member (wallet_id)` — the primary
  key leads with `cluster_id`, so the wallet→cluster direction had no index.
- `idx_wallet_tx_count` on `wallet (tx_count DESC NULLS LAST)` — matches the
  endpoint's own ordering, so the index is walked instead of 610,055 rows
  being sorted.
- Non-negative CHECKs on `transaction.value_wei`, `token_transfer.token_amount`,
  `flag_audit.framing_cost_wei` and `flag_audit.framing_gas`. Wei is unsigned
  on-chain, so a negative value is always a parse error — and a negative
  framing cost would make an attack look free.

### Migration 008 — `mv_cluster_flag_stats`

`/clusters/top-flagged` re-joined 780,644 membership rows to 128,637 flags on
every call. The view does that join once; the endpoint aggregates 395,206
small rows instead.

Refreshed `CONCURRENTLY` by `api/jobs.py` after any detector job, because the
trade is only honest if the view cannot go stale — a ranking showing the
previous run's counts is worse than the half-second it saves. A failed refresh
is logged and reported on the job (`cluster_stats_refreshed: false`), never
fatal: the flags are already committed by then.

### Two application-level query fixes

Neither is an index; both are the query asking for less.

1. **The subgraph endpoint now reads in two passes.** The ranking pass needs
   every member (connected expansion walks the whole cluster) but only needs
   `wallet_id` and PageRank. Addresses and verdicts are fetched only for the
   ~150 nodes actually kept. That removed the Seq Scan over all 610,055
   wallets and 26,444 unnecessary verdict lookups.
2. **The PageRank run is resolved once, not per wallet.** Inside a LATERAL the
   planner walked all 13 `analysis_run` rows for each of 26,594 members —
   345,722 index probes for 26,594 answers, 489 ms of the endpoint's 545 ms.
   With the run fixed up front, the existing
   `(run_id, wallet_id, metric_name)` unique index answers each member in one
   probe.

## Accuracy

`scripts/check_data_quality.py` (new) checks duplicate `tx_hash`, orphan rows,
token transfers with no parent transaction, snapshot lineage gaps, negative
wei, and verdicts without a cost.

**Result: 0 failures, 3 warnings.** The warnings are real data, reported
rather than "fixed":

1. **Snapshot 3 has no analysis run.** True — it was exported and benchmarked
   but never analysed.
2. **1,557 self-loop edges** (523 on snapshot 2). A CHECK constraint
   forbidding them was drafted and **deliberately dropped**: these are real
   transactions where `from == to` with value 0 (contract self-calls).
   Constraining them away would reject correct on-chain data. They are
   reported because a wallet pointing at itself still sits in the
   PageRank/Louvain input.
3. **`snapshot_edge.edge_weight` is `DOUBLE PRECISION` holding wei**, which
   rounds beyond ~15 significant digits where wei needs up to 27.

### The wei precision question, measured rather than assumed

The brief asked that wei columns be `NUMERIC(78,0)`. What is actually there:

| Column | Type | Verdict |
|---|---|---|
| `transaction.value_wei` | `NUMERIC(38,0)` | Sufficient. Total ETH supply is ~1.2×10²⁶ wei — 27 digits — so 38 cannot overflow. |
| `token_transfer.token_amount` | `NUMERIC(78,0)` | Full uint256, correct for tokens whose supply can exceed 10³⁸. |
| `wallet.*_wei`, `flag_audit.framing_cost_wei` | `NUMERIC(38,0)` | Sufficient, same reasoning. |
| `vertex_metric.metric_value` | `DOUBLE PRECISION` | Correct — PageRank scores are fractions, not currency. |
| `snapshot_edge.edge_weight` | `DOUBLE PRECISION` | **Lossy.** Holds summed wei. |

The lossy one was quantified rather than guessed at. Comparing the stored
double against the exact `NUMERIC` sum for every value-carrying edge on
snapshot 2:

- 107,195 edges carry value; **9,226 (8.6%) differ** from the exact sum.
- **Largest absolute error: 30,000,000 wei — 0.00000003 ETH.**
- Largest relative error: 5.0×10⁻¹⁵.

Not changed, deliberately. Widening the column rewrites a 174 MB table and
perturbs the PageRank/Louvain inputs that produce flags — a real risk to the
regression guard below — to recover an error eleven orders of magnitude below
anything that changes a ranking. Framing costs never read this column; they
read `transaction.value_wei`, which is `NUMERIC`.

## Regression guard

`scripts/audit_regression.py` captures every flag (id, detector, wallet,
score, threshold, token) and every latest verdict (verdict, framing cost,
method, minimal tx set) for a snapshot, and diffs two captures. Pinned to the
CPU backend, because CPU and GPU PageRank disagree on this data (see
`docs/benchmark_results.md`) and a cross-backend comparison would report a
difference that has nothing to do with the database.

Captured before migration 007 and again after every change above:

```
snapshot 1: 1,775 flags, 13 audits   — flags sha256 MATCH, audits sha256 MATCH
snapshot 2: 126,862 flags, 126 audits — flags sha256 MATCH, audits sha256 MATCH
Identical. The change did not alter any flag, verdict or framing cost.
```

The two restructured endpoints were additionally checked by output rather than
by timing: `/clusters/351299/subgraph` returns the same 150 nodes, 249 edges,
one connected component, zero isolated nodes and the same verdict counts as
before; `/clusters/top-flagged` returns the same eight clusters with identical
counts, verified against the live query across **all** clusters (a full outer
join difference query returned zero rows).

That check caught a real bug before it shipped. A first draft of the view
stored `count(DISTINCT wallet_id)` per flag run and summed it at query time,
which reported **26,761** flagged wallets for cluster 351299 where the correct
answer is **26,594** — summing per-run distinct counts double-counts any
wallet flagged by two detectors. The view is keyed at wallet grain so distinct
counting happens across the selected runs together.

## Not done, and why

- **Partitioning `transaction` / `token_transfer` by block number.** Not worth
  it at this size. The slow queries measured here are cluster and wallet
  joins; `transaction` never appeared in the slow list, and partitioning
  1.3 M rows would add planning overhead and migration risk for no measured
  gain. Worth revisiting an order of magnitude from now.
- **A materialized view for `/stats/overview`.** It measures 73 ms warm. A
  view would save perhaps 70 ms on one call per dashboard load, at the cost of
  another thing that can go stale.
- **Dropping unused indexes.** The two with zero scans are 24 kB and 8 kB and
  back query shapes this workload does not exercise (block-range scans,
  sign-in by email). Dropping them would save nothing and remove cover for
  real code paths.
- **A connection pool in the API.** Already present —
  `ThreadedConnectionPool(minconn=1, maxconn=10)` in `api/main.py`. Verified
  working during this pass: a Postgres restart left pooled connections dead
  and exactly one request returned 503 before the pool discarded the broken
  connection and recovered on its own.

## postgresql.conf — proposed, NOT applied

The server runs entirely on defaults, which assume a far smaller machine and a
spinning disk. These are suggestions for this laptop (16 GB RAM, SSD, single
user); **none have been applied**, per the brief.

| Setting | Current | Suggested | Why |
|---|---|---|---|
| `shared_buffers` | 128 MB | 2 GB | The working set is ~2 GB of tables; at 128 MB almost every scan re-reads from the OS cache. Q4 alone read 12,641 blocks from disk. |
| `effective_cache_size` | 4 GB | 8 GB | Tells the planner how much the OS is likely caching; too low biases it away from index scans. |
| `work_mem` | 4 MB | 32 MB | The top-flagged sort spilled across three parallel workers (3,124 kB + 1,992 kB + 543 kB). |
| `maintenance_work_mem` | 64 MB | 512 MB | Index builds and `REFRESH MATERIALIZED VIEW` — the 008 refresh is 395,206 rows. |
| `random_page_cost` | 4 | 1.1 | 4 encodes a spinning-disk seek penalty. On an SSD it makes the planner avoid index scans that are actually cheap. |

`random_page_cost` is the one most likely to change plan choices, so it should
be changed on its own with the regression guard re-run afterwards.
