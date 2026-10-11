# Database baseline (before Phase 10)

Measured 2026-10-11 on the live database (PostgreSQL 16.15, WSL2 Ubuntu-24.04),
with `pg_stat_statements` enabled and both `pg_stat_reset()` and
`pg_stat_statements_reset()` run immediately before, so every number below is
attributable to one replay of the real dashboard workload rather than to
whatever had happened earlier.

Workload: the endpoints the dashboard actually loads — Overview, Flags, Graph
and Multiview — against snapshot 2 (253,863 vertices, 355,989 edges, 126,862
flags).

## Table sizes

| Table | Live rows | Total | Indexes |
|---|---|---|---|
| `transaction` | 1,347,185 | 528 MB | 304 MB |
| `snapshot_edge_tx` | 2,152,275 | 454 MB | 229 MB |
| `vertex_metric` | 880,231 | 289 MB | 217 MB |
| `block` | 997,000 | 259 MB | 148 MB |
| `wallet` | 610,055 | 187 MB | 72 MB |
| `snapshot_edge` | 1,090,038 | 174 MB | 95 MB |
| `cluster_member` | 780,644 | 142 MB | 103 MB |
| `cluster` | 515,575 | 76 MB | 42 MB |
| `flag` | 128,637 | 17 MB | 6.6 MB |
| `flag_audit` | 256 | 664 kB | 32 kB |

Planner row estimates were checked against real counts (`pg_class.reltuples`
vs `count(*)`) and match — autovacuum has been keeping statistics current, so
stale statistics are **not** a factor here.

## Slowest queries by total time

| Total | Calls | Mean | Query |
|---|---|---|---|
| 1,807 ms | 3 | 602.5 ms | `/clusters/{id}/subgraph` — cluster members + PageRank + verdicts |
| 635 ms | 1 | 635.4 ms | `/clusters` (unfiltered, `ORDER BY run_id DESC LIMIT 100`) |
| 548 ms | 1 | 548.4 ms | `/clusters/top-flagged` |
| 133 ms | 3 | 44.5 ms | internal edges for a cluster subgraph |
| 66 ms | 1 | 66.1 ms | `/wallets?limit=200` (`ORDER BY tx_count DESC NULLS LAST`) |

## EXPLAIN (ANALYZE, BUFFERS) — what the planner was doing

### Q1 — cluster subgraph nodes, 1,037.9 ms

```
Sort (actual time=1023.422..1024.345 rows=26594)
  -> Nested Loop Left Join (actual time=251.257..1008.989 rows=26594)
       -> Hash Join (actual time=251.109..373.388 rows=26594)
            -> Seq Scan on wallet w (actual time=0.055..55.487 rows=610055)   <-- all 610k wallets
            -> Index Only Scan using cluster_member_pkey (rows=26594)
       -> Memoize -> ... vertex_metric lookup            (loops=26594, inner loops=345722)
       -> Memoize -> ... flag + flag_audit aggregate      (loops=26594)
Execution Time: 1037.899 ms
```

Two separate problems. It reads every one of the 610,055 wallets to get
addresses for 26,594 members, and it resolves PageRank per member by walking
all 13 `analysis_run` rows each time — 345,722 index probes for 26,594
answers. All of that to display **150** nodes.

### Q2 — `/clusters` unfiltered, 1,157.7 ms

```
Limit (actual time=1156.160..1156.182 rows=100)
  -> Nested Loop
       -> Index Scan Backward using analysis_run_pkey (rows=17)
       -> Materialize (actual time=0.005..40.560 rows=506699 loops=17)   <-- re-read 17 times
            -> Seq Scan on cluster c (rows=516622)
Execution Time: 1157.708 ms
```

`cluster` had no index on `run_id`, so ordering by it meant scanning all
516,622 rows into a Materialize that the nested loop then re-read 17 times —
to return 100 rows.

### Q3 — `/clusters/top-flagged`, 489.8 ms

```
Limit -> Sort -> GroupAggregate (rows=266)
  -> Nested Loop Left Join (rows=63431)
       -> Parallel Seq Scan on cluster_member cm (rows=780644)
       -> flag_audit LATERAL, once per flag
Execution Time: 489.8 ms
```

Joins 780,644 membership rows to 128,637 flags and runs the per-flag audit
lookup across all 63,431 matches, on every call — to rank 8 clusters.

### Q4 — `/wallets?limit=200`, 70.2 ms

```
Limit -> Gather Merge -> Sort (Sort Key: tx_count DESC NULLS LAST)
  Buffers: shared hit=2073 read=12641
Execution Time: 70.175 ms
```

No index on `tx_count`, so 610,055 rows are sorted (top-N heapsort) and 12,641
blocks read from disk to return 200.

## Index usage

Unused (zero scans): `idx_tx_block_brin`, `idx_app_user_email`. Both kept —
the BRIN index is tiny (24 kB) and exists for block-range queries the current
workload does not exercise, and the email index backs a sign-in lookup that no
test workload performs.

Missing, and visible in the plans above: no index on `cluster.run_id`, none on
`cluster_member.wallet_id` (the primary key leads with `cluster_id`, so the
reverse lookup had none), and none on `wallet.tx_count`.

## Server configuration (unchanged, defaults)

| Setting | Value | Note |
|---|---|---|
| `shared_buffers` | 128 MB | PostgreSQL default |
| `work_mem` | 4 MB | default |
| `maintenance_work_mem` | 64 MB | default |
| `effective_cache_size` | 4 GB | default |
| `random_page_cost` | 4 | assumes a spinning disk |

Tuning proposals are in `docs/perf/db_after.md` and were **not** applied.
