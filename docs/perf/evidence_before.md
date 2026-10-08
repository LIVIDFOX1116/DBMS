# Evidence query: baseline before any fix

Captured 2026-10-08, **before** any change to `db/queries/evidence.sql` / `graph/evidence.py` (no code changed).
Query: `db/queries/evidence.sql` verbatim, params `snapshot_id=1, seed_wallet_id=2, max_hops=10`.
DB: local `txgraph` (610,055 wallets, 1,347,185 transactions, `snapshot_edge_tx` = 2152275 rows across snapshots 1-3). Server: PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0, 64-bit.
Method: `EXPLAIN (ANALYZE, BUFFERS)` inside a read-only transaction (rolled back). Wallets are flagged wallets from snapshot 1
(this is *my* local DB; the 2,005-inbound / 26 s wallet in docs/FRAMECHECK.md is from the upstream DB, so wallet 26 with 1,997 inbound is the closest local analogue).

## Summary

| Wallet_id | Kind | Inbound tx (snap 1) | Evidence rows | Execution time | Notes |
|---|---|---|---|---|---|
| 126988 | normal | 2 | see plan | 2.28 s cold / 2.15 s warm | |
| 489 | normal | 37 | 500 | 6.60 s cold / 6.10 s warm | recursive term seq-scans `snapshot_edge_tx` 18x |
| 26 | high in-degree | 1,997 | 2,222 | **689.8 s** (TIMING OFF) | first attempt hit a 900 s statement_timeout; recursive term seq-scans `snapshot_edge_tx` 2,849x, 80.6M buffers read (~630 GB of page reads, mostly OS cache) |

## Finding (observation only; no fix applied)

Even a *normal* wallet costs 2-6 s because the query has no usable index path from `transaction` into `snapshot_edge_tx`:
both the hop=0 term and, far worse, the recursive LATERAL term plan as
`Seq Scan on snapshot_edge_tx` (2,152,275 rows) hash-joined to the per-wallet `transaction` bitmap scan, then `Index Scan snapshot_edge_pkey`
filtered by `snapshot_id`. The recursive term executes that full scan once per distinct-ish worktable row batch
(loops=2849 for wallet 26, 18 for wallet 489), so cost scales with (hop-0 rows x tables scan size), which is the cliff.
Indexes currently on the two tables:

- `idx_se_dst`: `CREATE INDEX idx_se_dst ON public.snapshot_edge USING btree (dst_wallet_id)`
- `idx_se_src`: `CREATE INDEX idx_se_src ON public.snapshot_edge USING btree (src_wallet_id)`
- `snapshot_edge_pkey`: `CREATE UNIQUE INDEX snapshot_edge_pkey ON public.snapshot_edge USING btree (edge_id)`
- `snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key`: `CREATE UNIQUE INDEX snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key ON public.snapshot_edge USING btree (snapshot_id, src_wallet_id, dst_wallet_id)`
- `snapshot_edge_tx_pkey`: `CREATE UNIQUE INDEX snapshot_edge_tx_pkey ON public.snapshot_edge_tx USING btree (snapshot_edge_id, tx_hash)`

No index exists on `snapshot_edge_tx(tx_hash)`.

## Wallet 126988: normal (2 inbound tx in snapshot 1)

### cold-ish (1st run): wall 2.29s

```
Unique  (cost=608797.95..608798.18 rows=19 width=312) (actual time=2257.494..2257.758 rows=4 loops=1)
  Buffers: shared hit=8757 read=111693
  CTE evidence
    ->  Recursive Union  (cost=1711.32..608797.16 rows=19 width=153) (actual time=946.501..2257.594 rows=4 loops=1)
          Buffers: shared hit=8745 read=111693
          ->  Gather  (cost=1711.32..41788.73 rows=9 width=153) (actual time=946.494..947.223 rows=2 loops=1)
                Workers Planned: 2
                Workers Launched: 2
                Buffers: shared hit=2799 read=28062
                ->  Nested Loop  (cost=711.32..40787.83 rows=4 width=153) (actual time=785.884..920.276 rows=1 loops=3)
                      Buffers: shared hit=2799 read=28062
                      ->  Hash Join  (cost=710.89..40729.77 rows=123 width=93) (actual time=697.380..915.821 rows=168 loops=3)
                            Hash Cond: (st.tx_hash = t.tx_hash)
                            Buffers: shared hit=857 read=27982
                            ->  Parallel Seq Scan on snapshot_edge_tx st  (cost=0.00..37664.81 rows=896781 width=75) (actual time=0.054..110.616 rows=717425 loops=3)
                                  Buffers: shared hit=736 read=27961
                            ->  Hash  (cost=708.58..708.58 rows=185 width=85) (actual time=634.641..634.647 rows=501 loops=3)
                                  Buckets: 1024  Batches: 1  Memory Usage: 68kB
                                  Buffers: shared hit=61 read=21
                                  ->  Bitmap Heap Scan on transaction t  (cost=10.34..708.58 rows=185 width=85) (actual time=634.362..634.495 rows=501 loops=3)
                                        Recheck Cond: ((from_wallet_id = 126988) OR (to_wallet_id = 126988))
                                        Heap Blocks: exact=15
                                        Buffers: shared hit=61 read=21
                                        ->  BitmapOr  (cost=10.34..10.34 rows=185 width=0) (actual time=0.680..0.683 rows=0 loops=3)
                                              Buffers: shared hit=31 read=6
                                              ->  Bitmap Index Scan on idx_tx_from_block  (cost=0.00..4.68 rows=34 width=0) (actual time=0.316..0.316 rows=0 loops=3)
                                                    Index Cond: (from_wallet_id = 126988)
                                                    Buffers: shared hit=18 read=2
                                              ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.56 rows=151 width=0) (actual time=0.363..0.363 rows=501 loops=3)
                                                    Index Cond: (to_wallet_id = 126988)
                                                    Buffers: shared hit=13 read=4
                      ->  Index Scan using snapshot_edge_pkey on snapshot_edge se  (cost=0.43..0.47 rows=1 width=8) (actual time=0.026..0.026 rows=0 loops=505)
                            Index Cond: (edge_id = st.snapshot_edge_id)
                            Filter: (snapshot_id = 1)
                            Rows Removed by Filter: 1
                            Buffers: shared hit=1942 read=80
          ->  Nested Loop  (cost=56697.21..56700.82 rows=1 width=153) (actual time=424.747..655.172 rows=1 loops=2)
                Buffers: shared hit=5946 read=83631
                ->  WorkTable Scan on evidence e  (cost=0.00..3.60 rows=1 width=44) (actual time=0.013..0.018 rows=2 loops=2)
                      Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                ->  Limit  (cost=56697.21..56697.21 rows=1 width=85) (actual time=327.561..327.564 rows=0 loops=4)
                      Buffers: shared hit=5946 read=83631
                      ->  Sort  (cost=56697.21..56697.23 rows=9 width=85) (actual time=327.559..327.561 rows=0 loops=4)
                            Sort Key: t_1.block_number, t_1.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=5946 read=83631
                            ->  Nested Loop  (cost=691.69..56697.16 rows=9 width=85) (actual time=128.877..327.517 rows=65 loops=4)
                                  Buffers: shared hit=5943 read=83631
                                  ->  Hash Join  (cost=691.26..56560.76 rows=289 width=93) (actual time=24.552..326.464 rows=214 loops=4)
                                        Hash Cond: (st_1.tx_hash = t_1.tx_hash)
                                        Buffers: shared hit=2532 read=83610
                                        ->  Seq Scan on snapshot_edge_tx st_1  (cost=0.00..50219.75 rows=2152275 width=75) (actual time=0.029..189.349 rows=2152275 loops=3)
                                              Buffers: shared hit=2496 read=83595
                                        ->  Hash  (cost=689.00..689.00 rows=181 width=85) (actual time=0.699..0.700 rows=81 loops=4)
                                              Buckets: 1024  Batches: 1  Memory Usage: 39kB
                                              Buffers: shared hit=36 read=15
                                              ->  Bitmap Heap Scan on transaction t_1  (cost=5.83..689.00 rows=181 width=85) (actual time=0.603..0.671 rows=81 loops=4)
                                                    Recheck Cond: (to_wallet_id = e.from_wallet_id)
                                                    Heap Blocks: exact=39
                                                    Buffers: shared hit=36 read=15
                                                    ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.79 rows=181 width=0) (actual time=0.518..0.518 rows=81 loops=4)
                                                          Index Cond: (to_wallet_id = e.from_wallet_id)
                                                          Buffers: shared hit=8 read=4
                                  ->  Index Scan using snapshot_edge_pkey on snapshot_edge se_1  (cost=0.43..0.47 rows=1 width=8) (actual time=0.004..0.004 rows=0 loops=858)
                                        Index Cond: (edge_id = st_1.snapshot_edge_id)
                                        Filter: (snapshot_id = 1)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=3411 read=21
  ->  Sort  (cost=0.78..0.83 rows=19 width=312) (actual time=2257.492..2257.494 rows=4 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=8757 read=111693
        ->  CTE Scan on evidence  (cost=0.00..0.38 rows=19 width=312) (actual time=946.524..2257.406 rows=4 loops=1)
              Buffers: shared hit=8745 read=111693
Planning:
  Buffers: shared hit=444
Planning Time: 2.426 ms
JIT:
  Functions: 85
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 10.994 ms, Inlining 631.904 ms, Optimization 762.785 ms, Emission 506.860 ms, Total 1912.544 ms
Execution Time: 2282.885 ms
```

### warm (2nd run): wall 2.16s

```
Unique  (cost=608797.95..608798.18 rows=19 width=312) (actual time=2152.559..2152.699 rows=4 loops=1)
  Buffers: shared hit=9388 read=111044
  CTE evidence
    ->  Recursive Union  (cost=1711.32..608797.16 rows=19 width=153) (actual time=716.645..2152.631 rows=4 loops=1)
          Buffers: shared hit=9388 read=111044
          ->  Gather  (cost=1711.32..41788.73 rows=9 width=153) (actual time=716.641..716.865 rows=2 loops=1)
                Workers Planned: 2
                Workers Launched: 2
                Buffers: shared hit=3025 read=27833
                ->  Nested Loop  (cost=711.32..40787.83 rows=4 width=153) (actual time=487.163..700.607 rows=1 loops=3)
                      Buffers: shared hit=3025 read=27833
                      ->  Hash Join  (cost=710.89..40729.77 rows=123 width=93) (actual time=465.200..699.499 rows=168 loops=3)
                            Hash Cond: (st.tx_hash = t.tx_hash)
                            Buffers: shared hit=1003 read=27833
                            ->  Parallel Seq Scan on snapshot_edge_tx st  (cost=0.00..37664.81 rows=896781 width=75) (actual time=0.036..101.461 rows=717425 loops=3)
                                  Buffers: shared hit=864 read=27833
                            ->  Hash  (cost=708.58..708.58 rows=185 width=85) (actual time=457.396..457.398 rows=501 loops=3)
                                  Buckets: 1024  Batches: 1  Memory Usage: 68kB
                                  Buffers: shared hit=79
                                  ->  Bitmap Heap Scan on transaction t  (cost=10.34..708.58 rows=185 width=85) (actual time=457.106..457.225 rows=501 loops=3)
                                        Recheck Cond: ((from_wallet_id = 126988) OR (to_wallet_id = 126988))
                                        Heap Blocks: exact=15
                                        Buffers: shared hit=79
                                        ->  BitmapOr  (cost=10.34..10.34 rows=185 width=0) (actual time=0.053..0.054 rows=0 loops=3)
                                              Buffers: shared hit=34
                                              ->  Bitmap Index Scan on idx_tx_from_block  (cost=0.00..4.68 rows=34 width=0) (actual time=0.023..0.023 rows=0 loops=3)
                                                    Index Cond: (from_wallet_id = 126988)
                                                    Buffers: shared hit=17
                                              ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.56 rows=151 width=0) (actual time=0.029..0.029 rows=501 loops=3)
                                                    Index Cond: (to_wallet_id = 126988)
                                                    Buffers: shared hit=17
                      ->  Index Scan using snapshot_edge_pkey on snapshot_edge se  (cost=0.43..0.47 rows=1 width=8) (actual time=0.006..0.006 rows=0 loops=505)
                            Index Cond: (edge_id = st.snapshot_edge_id)
                            Filter: (snapshot_id = 1)
                            Rows Removed by Filter: 1
                            Buffers: shared hit=2022
          ->  Nested Loop  (cost=56697.21..56700.82 rows=1 width=153) (actual time=492.283..717.872 rows=1 loops=2)
                Buffers: shared hit=6363 read=83211
                ->  WorkTable Scan on evidence e  (cost=0.00..3.60 rows=1 width=44) (actual time=0.012..0.015 rows=2 loops=2)
                      Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                ->  Limit  (cost=56697.21..56697.21 rows=1 width=85) (actual time=358.914..358.915 rows=0 loops=4)
                      Buffers: shared hit=6363 read=83211
                      ->  Sort  (cost=56697.21..56697.23 rows=9 width=85) (actual time=358.908..358.909 rows=0 loops=4)
                            Sort Key: t_1.block_number, t_1.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=6363 read=83211
                            ->  Nested Loop  (cost=691.69..56697.16 rows=9 width=85) (actual time=118.007..358.881 rows=65 loops=4)
                                  Buffers: shared hit=6363 read=83211
                                  ->  Hash Join  (cost=691.26..56560.76 rows=289 width=93) (actual time=21.048..358.297 rows=214 loops=4)
                                        Hash Cond: (st_1.tx_hash = t_1.tx_hash)
                                        Buffers: shared hit=2931 read=83211
                                        ->  Seq Scan on snapshot_edge_tx st_1  (cost=0.00..50219.75 rows=2152275 width=75) (actual time=0.028..211.965 rows=2152275 loops=3)
                                              Buffers: shared hit=2880 read=83211
                                        ->  Hash  (cost=689.00..689.00 rows=181 width=85) (actual time=0.077..0.077 rows=81 loops=4)
                                              Buckets: 1024  Batches: 1  Memory Usage: 39kB
                                              Buffers: shared hit=51
                                              ->  Bitmap Heap Scan on transaction t_1  (cost=5.83..689.00 rows=181 width=85) (actual time=0.023..0.057 rows=81 loops=4)
                                                    Recheck Cond: (to_wallet_id = e.from_wallet_id)
                                                    Heap Blocks: exact=39
                                                    Buffers: shared hit=51
                                                    ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.79 rows=181 width=0) (actual time=0.012..0.012 rows=81 loops=4)
                                                          Index Cond: (to_wallet_id = e.from_wallet_id)
                                                          Buffers: shared hit=12
                                  ->  Index Scan using snapshot_edge_pkey on snapshot_edge se_1  (cost=0.43..0.47 rows=1 width=8) (actual time=0.002..0.002 rows=0 loops=858)
                                        Index Cond: (edge_id = st_1.snapshot_edge_id)
                                        Filter: (snapshot_id = 1)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=3432
  ->  Sort  (cost=0.78..0.83 rows=19 width=312) (actual time=2152.557..2152.558 rows=4 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=9388 read=111044
        ->  CTE Scan on evidence  (cost=0.00..0.38 rows=19 width=312) (actual time=716.651..2152.538 rows=4 loops=1)
              Buffers: shared hit=9388 read=111044
Planning:
  Buffers: shared hit=52
Planning Time: 0.661 ms
JIT:
  Functions: 85
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 4.610 ms, Inlining 336.388 ms, Optimization 608.422 ms, Emission 426.687 ms, Total 1376.107 ms
Execution Time: 2154.330 ms
```

## Wallet 489: normal (37 inbound tx; 500 evidence rows)

### cold-ish (1st run): wall 6.62s

```
Unique  (cost=608797.95..608798.18 rows=19 width=312) (actual time=6592.315..6592.505 rows=500 loops=1)
  Buffers: shared hit=39589 read=535226
  CTE evidence
    ->  Recursive Union  (cost=1711.32..608797.16 rows=19 width=153) (actual time=362.958..6592.022 rows=500 loops=1)
          Buffers: shared hit=39577 read=535226
          ->  Gather  (cost=1711.32..41788.73 rows=9 width=153) (actual time=362.955..363.076 rows=500 loops=1)
                Workers Planned: 2
                Workers Launched: 2
                Buffers: shared hit=6521 read=28314
                ->  Nested Loop  (cost=711.32..40787.83 rows=4 width=153) (actual time=238.742..353.219 rows=167 loops=3)
                      Buffers: shared hit=6521 read=28314
                      ->  Hash Join  (cost=710.89..40729.77 rows=123 width=93) (actual time=238.373..348.724 rows=500 loops=3)
                            Hash Cond: (st.tx_hash = t.tx_hash)
                            Buffers: shared hit=595 read=28238
                            ->  Parallel Seq Scan on snapshot_edge_tx st  (cost=0.00..37664.81 rows=896781 width=75) (actual time=0.021..47.548 rows=717425 loops=3)
                                  Buffers: shared hit=480 read=28217
                            ->  Hash  (cost=708.58..708.58 rows=185 width=85) (actual time=233.587..233.589 rows=500 loops=3)
                                  Buckets: 1024  Batches: 1  Memory Usage: 68kB
                                  Buffers: shared hit=55 read=21
                                  ->  Bitmap Heap Scan on transaction t  (cost=10.34..708.58 rows=185 width=85) (actual time=233.373..233.493 rows=500 loops=3)
                                        Recheck Cond: ((from_wallet_id = 489) OR (to_wallet_id = 489))
                                        Heap Blocks: exact=13
                                        Buffers: shared hit=55 read=21
                                        ->  BitmapOr  (cost=10.34..10.34 rows=185 width=0) (actual time=0.557..0.557 rows=0 loops=3)
                                              Buffers: shared hit=29 read=8
                                              ->  Bitmap Index Scan on idx_tx_from_block  (cost=0.00..4.68 rows=34 width=0) (actual time=0.417..0.417 rows=463 loops=3)
                                                    Index Cond: (from_wallet_id = 489)
                                                    Buffers: shared hit=21 read=5
                                              ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.56 rows=151 width=0) (actual time=0.139..0.139 rows=37 loops=3)
                                                    Index Cond: (to_wallet_id = 489)
                                                    Buffers: shared hit=8 read=3
                      ->  Index Scan using snapshot_edge_pkey on snapshot_edge se  (cost=0.43..0.47 rows=1 width=8) (actual time=0.009..0.009 rows=0 loops=1500)
                            Index Cond: (edge_id = st.snapshot_edge_id)
                            Filter: (snapshot_id = 1)
                            Rows Removed by Filter: 1
                            Buffers: shared hit=5926 read=76
          ->  Nested Loop  (cost=56697.21..56700.82 rows=1 width=153) (actual time=6228.870..6228.872 rows=0 loops=1)
                Buffers: shared hit=33056 read=506912
                ->  WorkTable Scan on evidence e  (cost=0.00..3.60 rows=1 width=44) (actual time=0.024..0.143 rows=37 loops=1)
                      Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                      Rows Removed by Filter: 463
                ->  Limit  (cost=56697.21..56697.21 rows=1 width=85) (actual time=168.342..168.342 rows=0 loops=37)
                      Buffers: shared hit=33056 read=506912
                      ->  Sort  (cost=56697.21..56697.23 rows=9 width=85) (actual time=168.340..168.340 rows=0 loops=37)
                            Sort Key: t_1.block_number, t_1.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=33056 read=506912
                            ->  Nested Loop  (cost=691.69..56697.16 rows=9 width=85) (actual time=168.334..168.334 rows=0 loops=37)
                                  Buffers: shared hit=33053 read=506912
                                  ->  Hash Join  (cost=691.26..56560.76 rows=289 width=93) (actual time=36.490..167.115 rows=154 loops=37)
                                        Hash Cond: (st_1.tx_hash = t_1.tx_hash)
                                        Buffers: shared hit=10721 read=506496
                                        ->  Seq Scan on snapshot_edge_tx st_1  (cost=0.00..50219.75 rows=2152275 width=75) (actual time=0.006..138.729 rows=2152275 loops=18)
                                              Buffers: shared hit=10368 read=506178
                                        ->  Hash  (cost=689.00..689.00 rows=181 width=85) (actual time=0.372..0.372 rows=94 loops=37)
                                              Buckets: 1024  Batches: 1  Memory Usage: 82kB
                                              Buffers: shared hit=353 read=318
                                              ->  Bitmap Heap Scan on transaction t_1  (cost=5.83..689.00 rows=181 width=85) (actual time=0.301..0.354 rows=94 loops=37)
                                                    Recheck Cond: (to_wallet_id = e.from_wallet_id)
                                                    Heap Blocks: exact=551
                                                    Buffers: shared hit=353 read=318
                                                    ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.79 rows=181 width=0) (actual time=0.293..0.293 rows=94 loops=37)
                                                          Index Cond: (to_wallet_id = e.from_wallet_id)
                                                          Buffers: shared hit=89 read=31
                                  ->  Index Scan using snapshot_edge_pkey on snapshot_edge se_1  (cost=0.43..0.47 rows=1 width=8) (actual time=0.008..0.008 rows=0 loops=5687)
                                        Index Cond: (edge_id = st_1.snapshot_edge_id)
                                        Filter: (snapshot_id = 1)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=22332 read=416
  ->  Sort  (cost=0.78..0.83 rows=19 width=312) (actual time=6592.314..6592.337 rows=500 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 79kB
        Buffers: shared hit=39589 read=535226
        ->  CTE Scan on evidence  (cost=0.00..0.38 rows=19 width=312) (actual time=362.966..6592.191 rows=500 loops=1)
              Buffers: shared hit=39577 read=535226
Planning:
  Buffers: shared hit=443 read=1
Planning Time: 1.183 ms
JIT:
  Functions: 85
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 2.926 ms, Inlining 226.602 ms, Optimization 272.026 ms, Emission 199.945 ms, Total 701.499 ms
Execution Time: 6604.679 ms
```

### warm (2nd run): wall 6.10s

```
Unique  (cost=608797.95..608798.18 rows=19 width=312) (actual time=6101.129..6101.317 rows=500 loops=1)
  Buffers: shared hit=42834 read=531963
  CTE evidence
    ->  Recursive Union  (cost=1711.32..608797.16 rows=19 width=153) (actual time=436.899..6100.961 rows=500 loops=1)
          Buffers: shared hit=42834 read=531963
          ->  Gather  (cost=1711.32..41788.73 rows=9 width=153) (actual time=436.895..437.342 rows=500 loops=1)
                Workers Planned: 2
                Workers Launched: 2
                Buffers: shared hit=6743 read=28089
                ->  Nested Loop  (cost=711.32..40787.83 rows=4 width=153) (actual time=280.412..350.243 rows=167 loops=3)
                      Buffers: shared hit=6743 read=28089
                      ->  Hash Join  (cost=710.89..40729.77 rows=123 width=93) (actual time=247.450..349.432 rows=500 loops=3)
                            Hash Cond: (st.tx_hash = t.tx_hash)
                            Buffers: shared hit=741 read=28089
                            ->  Parallel Seq Scan on snapshot_edge_tx st  (cost=0.00..37664.81 rows=896781 width=75) (actual time=0.023..48.405 rows=717425 loops=3)
                                  Buffers: shared hit=608 read=28089
                            ->  Hash  (cost=708.58..708.58 rows=185 width=85) (actual time=230.675..230.676 rows=500 loops=3)
                                  Buckets: 1024  Batches: 1  Memory Usage: 68kB
                                  Buffers: shared hit=73
                                  ->  Bitmap Heap Scan on transaction t  (cost=10.34..708.58 rows=185 width=85) (actual time=230.505..230.586 rows=500 loops=3)
                                        Recheck Cond: ((from_wallet_id = 489) OR (to_wallet_id = 489))
                                        Heap Blocks: exact=13
                                        Buffers: shared hit=73
                                        ->  BitmapOr  (cost=10.34..10.34 rows=185 width=0) (actual time=0.046..0.047 rows=0 loops=3)
                                              Buffers: shared hit=34
                                              ->  Bitmap Index Scan on idx_tx_from_block  (cost=0.00..4.68 rows=34 width=0) (actual time=0.038..0.038 rows=463 loops=3)
                                                    Index Cond: (from_wallet_id = 489)
                                                    Buffers: shared hit=23
                                              ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.56 rows=151 width=0) (actual time=0.007..0.008 rows=37 loops=3)
                                                    Index Cond: (to_wallet_id = 489)
                                                    Buffers: shared hit=11
                      ->  Index Scan using snapshot_edge_pkey on snapshot_edge se  (cost=0.43..0.47 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=1500)
                            Index Cond: (edge_id = st.snapshot_edge_id)
                            Filter: (snapshot_id = 1)
                            Rows Removed by Filter: 1
                            Buffers: shared hit=6002
          ->  Nested Loop  (cost=56697.21..56700.82 rows=1 width=153) (actual time=5663.543..5663.546 rows=0 loops=1)
                Buffers: shared hit=36091 read=503874
                ->  WorkTable Scan on evidence e  (cost=0.00..3.60 rows=1 width=44) (actual time=0.017..0.129 rows=37 loops=1)
                      Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                      Rows Removed by Filter: 463
                ->  Limit  (cost=56697.21..56697.21 rows=1 width=85) (actual time=153.064..153.064 rows=0 loops=37)
                      Buffers: shared hit=36091 read=503874
                      ->  Sort  (cost=56697.21..56697.23 rows=9 width=85) (actual time=153.062..153.062 rows=0 loops=37)
                            Sort Key: t_1.block_number, t_1.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=36091 read=503874
                            ->  Nested Loop  (cost=691.69..56697.16 rows=9 width=85) (actual time=153.057..153.058 rows=0 loops=37)
                                  Buffers: shared hit=36091 read=503874
                                  ->  Hash Join  (cost=691.26..56560.76 rows=289 width=93) (actual time=35.191..152.818 rows=154 loops=37)
                                        Hash Cond: (st_1.tx_hash = t_1.tx_hash)
                                        Buffers: shared hit=13343 read=503874
                                        ->  Seq Scan on snapshot_edge_tx st_1  (cost=0.00..50219.75 rows=2152275 width=75) (actual time=0.006..132.343 rows=2152275 loops=18)
                                              Buffers: shared hit=12672 read=503874
                                        ->  Hash  (cost=689.00..689.00 rows=181 width=85) (actual time=0.046..0.046 rows=94 loops=37)
                                              Buckets: 1024  Batches: 1  Memory Usage: 82kB
                                              Buffers: shared hit=671
                                              ->  Bitmap Heap Scan on transaction t_1  (cost=5.83..689.00 rows=181 width=85) (actual time=0.011..0.030 rows=94 loops=37)
                                                    Recheck Cond: (to_wallet_id = e.from_wallet_id)
                                                    Heap Blocks: exact=551
                                                    Buffers: shared hit=671
                                                    ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.79 rows=181 width=0) (actual time=0.007..0.007 rows=94 loops=37)
                                                          Index Cond: (to_wallet_id = e.from_wallet_id)
                                                          Buffers: shared hit=120
                                  ->  Index Scan using snapshot_edge_pkey on snapshot_edge se_1  (cost=0.43..0.47 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=5687)
                                        Index Cond: (edge_id = st_1.snapshot_edge_id)
                                        Filter: (snapshot_id = 1)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=22748
  ->  Sort  (cost=0.78..0.83 rows=19 width=312) (actual time=6101.128..6101.148 rows=500 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 79kB
        Buffers: shared hit=42834 read=531963
        ->  CTE Scan on evidence  (cost=0.00..0.38 rows=19 width=312) (actual time=436.903..6101.010 rows=500 loops=1)
              Buffers: shared hit=42834 read=531963
Planning:
  Buffers: shared hit=52
Planning Time: 0.640 ms
JIT:
  Functions: 85
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 4.436 ms, Inlining 193.610 ms, Optimization 301.179 ms, Emission 196.702 ms, Total 695.927 ms
Execution Time: 6103.258 ms
```

## Wallet 26: HIGH in-degree (1,997 inbound tx in snapshot 1)

### single run, TIMING OFF: wall 707.44s

```
Sort  (cost=4034172.01..4034172.51 rows=200 width=312) (actual rows=2222 loops=1)
  Sort Key: evidence.block_number, evidence.hop
  Sort Method: quicksort  Memory: 357kB
  Buffers: shared hit=3040012 read=80671059
  CTE evidence
    ->  Recursive Union  (cost=20506.59..4034146.32 rows=535 width=153) (actual rows=4067 loops=1)
          Buffers: shared hit=3040006 read=80671059
          ->  Gather  (cost=20506.59..63475.27 rows=465 width=153) (actual rows=1997 loops=1)
                Workers Planned: 2
                Workers Launched: 2
                Buffers: shared hit=66456 read=30053
                ->  Nested Loop  (cost=19506.59..62428.77 rows=194 width=153) (actual rows=666 loops=3)
                      Buffers: shared hit=66456 read=30053
                      ->  Parallel Hash Join  (cost=19506.17..59525.04 rows=6151 width=93) (actual rows=5390 loops=3)
                            Hash Cond: (st.tx_hash = t.tx_hash)
                            Buffers: shared hit=1770 read=30053
                            ->  Parallel Seq Scan on snapshot_edge_tx st  (cost=0.00..37664.81 rows=896781 width=75) (actual rows=717425 loops=3)
                                  Buffers: shared hit=296 read=28401
                            ->  Parallel Hash  (cost=19458.04..19458.04 rows=3850 width=85) (actual rows=2955 loops=3)
                                  Buckets: 16384  Batches: 1  Memory Usage: 1312kB
                                  Buffers: shared hit=1414 read=1652
                                  ->  Parallel Bitmap Heap Scan on transaction t  (cost=126.78..19458.04 rows=3850 width=85) (actual rows=2955 loops=3)
                                        Recheck Cond: ((from_wallet_id = 26) OR (to_wallet_id = 26))
                                        Heap Blocks: exact=1
                                        Buffers: shared hit=1414 read=1652
                                        ->  BitmapOr  (cost=126.78..126.78 rows=9240 width=0) (actual rows=0 loops=1)
                                              Buffers: shared hit=5 read=10
                                              ->  Bitmap Index Scan on idx_tx_from_block  (cost=0.00..4.68 rows=34 width=0) (actual rows=0 loops=1)
                                                    Index Cond: (from_wallet_id = 26)
                                                    Buffers: shared hit=5 read=1
                                              ->  Bitmap Index Scan on idx_tx_to  (cost=0.00..117.47 rows=9206 width=0) (actual rows=8866 loops=1)
                                                    Index Cond: (to_wallet_id = 26)
                                                    Buffers: shared read=9
                      ->  Index Scan using snapshot_edge_pkey on snapshot_edge se  (cost=0.43..0.47 rows=1 width=8) (actual rows=0 loops=16171)
                            Index Cond: (edge_id = st.snapshot_edge_id)
                            Filter: (snapshot_id = 1)
                            Rows Removed by Filter: 1
                            Buffers: shared hit=64686
          ->  Nested Loop  (cost=56697.21..397066.57 rows=7 width=153) (actual rows=518 loops=4)
                Buffers: shared hit=2973550 read=80641006
                ->  WorkTable Scan on evidence e  (cost=0.00..186.00 rows=7 width=44) (actual rows=935 loops=4)
                      Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                      Rows Removed by Filter: 82
                ->  Limit  (cost=56697.21..56697.21 rows=1 width=85) (actual rows=1 loops=3740)
                      Buffers: shared hit=2973550 read=80641006
                      ->  Sort  (cost=56697.21..56697.23 rows=9 width=85) (actual rows=1 loops=3740)
                            Sort Key: t_1.block_number, t_1.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=2973550 read=80641006
                            ->  Nested Loop  (cost=691.69..56697.16 rows=9 width=85) (actual rows=23 loops=3740)
                                  Buffers: shared hit=2973547 read=80641006
                                  ->  Hash Join  (cost=691.26..56560.76 rows=289 width=93) (actual rows=121 loops=3740)
                                        Hash Cond: (st_1.tx_hash = t_1.tx_hash)
                                        Buffers: shared hit=1158824 read=80640989
                                        ->  Seq Scan on snapshot_edge_tx st_1  (cost=0.00..50219.75 rows=2152275 width=75) (actual rows=2152275 loops=2849)
                                              Buffers: shared hit=1116808 read=80640945
                                        ->  Hash  (cost=689.00..689.00 rows=181 width=85) (actual rows=58 loops=3740)
                                              Buckets: 1024  Batches: 1  Memory Usage: 82kB
                                              Buffers: shared hit=42016 read=44
                                              ->  Bitmap Heap Scan on transaction t_1  (cost=5.83..689.00 rows=181 width=85) (actual rows=58 loops=3740)
                                                    Recheck Cond: (to_wallet_id = e.from_wallet_id)
                                                    Heap Blocks: exact=29898
                                                    Buffers: shared hit=42016 read=44
                                                    ->  Bitmap Index Scan on idx_tx_to_block  (cost=0.00..5.79 rows=181 width=0) (actual rows=58 loops=3740)
                                                          Index Cond: (to_wallet_id = e.from_wallet_id)
                                                          Buffers: shared hit=12148 read=14
                                  ->  Index Scan using snapshot_edge_pkey on snapshot_edge se_1  (cost=0.43..0.47 rows=1 width=8) (actual rows=0 loops=453685)
                                        Index Cond: (edge_id = st_1.snapshot_edge_id)
                                        Filter: (snapshot_id = 1)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=1814723 read=17
  ->  HashAggregate  (cost=16.05..18.05 rows=200 width=312) (actual rows=2222 loops=1)
        Group Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Batches: 1  Memory Usage: 641kB
        Buffers: shared hit=3040006 read=80671059
        ->  CTE Scan on evidence  (cost=0.00..10.70 rows=535 width=312) (actual rows=4067 loops=1)
              Buffers: shared hit=3040006 read=80671059
Planning:
  Buffers: shared hit=444
Planning Time: 2.851 ms
JIT:
  Functions: 87
  Options: Inlining true, Optimization true, Expressions true, Deforming true
Execution Time: 689782.737 ms
```
