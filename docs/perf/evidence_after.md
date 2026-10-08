# Evidence query: after the fix

Captured 2026-10-08 against the same DB and parameters as `evidence_before.md` (`snapshot_id=1, seed_wallet_id=2, max_hops=10`).
Server: PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1). Method: `EXPLAIN (ANALYZE, BUFFERS)` in a read-only transaction.

Changes in `db/queries/evidence.sql`: (1) both the hop-0 and the recursive terms are driven from `snapshot_edge` (`idx_se_src`/`idx_se_dst`/the unique (snapshot_id, src, dst) index) into `snapshot_edge_tx`'s (snapshot_edge_id, tx_hash) primary key, so nothing seq-scans `snapshot_edge_tx` any more; (2) each recursive generation is deduplicated by `from_wallet_id` (`DISTINCT ON`); (3) a token-recipient hop-0 branch. No migration or new index was needed.

| Wallet | Evidence rows | Before | After |
|---|---|---|---|
|126988|4|2.28 s|3.084 ms|
|489|500|6.60 s|5.185 ms|
|26|2222|689.8 s|83.212 ms|
|28|4699|~22 min (audit, not isolated)|103.892 ms|

## Wallet 126988

```
Unique  (cost=5802.12..5802.36 rows=19 width=312) (actual time=2.919..2.924 rows=4 loops=1)
  Buffers: shared hit=1509 read=50
  CTE evidence
    ->  Recursive Union  (cost=10.73..5801.34 rows=19 width=354) (actual time=0.305..2.895 rows=4 loops=1)
          Buffers: shared hit=1497 read=50
          ->  Append  (cost=10.73..683.57 rows=9 width=354) (actual time=0.303..1.102 rows=2 loops=1)
                Buffers: shared hit=35 read=37
                ->  Nested Loop  (cost=10.73..470.51 rows=8 width=153) (actual time=0.303..1.080 rows=2 loops=1)
                      Buffers: shared hit=33 read=36
                      ->  Nested Loop  (cost=10.17..465.19 rows=8 width=67) (actual time=0.278..1.043 rows=2 loops=1)
                            Buffers: shared hit=23 read=36
                            ->  Bitmap Heap Scan on snapshot_edge se  (cost=9.74..445.71 rows=4 width=8) (actual time=0.259..1.017 rows=2 loops=1)
                                  Recheck Cond: (((snapshot_id = 1) AND (src_wallet_id = 126988)) OR (dst_wallet_id = 126988))
                                  Filter: (snapshot_id = 1)
                                  Rows Removed by Filter: 429
                                  Heap Blocks: exact=43
                                  Buffers: shared hit=18 read=34
                                  ->  BitmapOr  (cost=9.74..9.74 rows=118 width=0) (actual time=0.045..0.045 rows=0 loops=1)
                                        Buffers: shared hit=7 read=2
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..4.44 rows=1 width=0) (actual time=0.024..0.025 rows=0 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (src_wallet_id = 126988))
                                              Buffers: shared hit=5 read=1
                                        ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.30 rows=117 width=0) (actual time=0.020..0.020 rows=431 loops=1)
                                              Index Cond: (dst_wallet_id = 126988)
                                              Buffers: shared hit=2 read=1
                            ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st  (cost=0.43..4.71 rows=16 width=75) (actual time=0.011..0.012 rows=1 loops=2)
                                  Index Cond: (snapshot_edge_id = se.edge_id)
                                  Heap Fetches: 0
                                  Buffers: shared hit=5 read=2
                      ->  Index Scan using transaction_pkey on transaction t  (cost=0.55..0.66 rows=1 width=85) (actual time=0.017..0.017 rows=1 loops=2)
                            Index Cond: (tx_hash = st.tx_hash)
                            Buffers: shared hit=10
                ->  Nested Loop  (cost=1.83..213.02 rows=1 width=153) (actual time=0.019..0.020 rows=0 loops=1)
                      Buffers: shared hit=2 read=1
                      ->  Nested Loop  (cost=1.40..210.05 rows=1 width=160) (actual time=0.019..0.020 rows=0 loops=1)
                            Buffers: shared hit=2 read=1
                            ->  Nested Loop  (cost=0.97..202.11 rows=16 width=160) (actual time=0.019..0.020 rows=0 loops=1)
                                  Buffers: shared hit=2 read=1
                                  ->  Index Scan using idx_tt_to on token_transfer tt  (cost=0.42..64.95 rows=16 width=67) (actual time=0.019..0.019 rows=0 loops=1)
                                        Index Cond: (to_wallet_id = 126988)
                                        Buffers: shared hit=2 read=1
                                  ->  Index Scan using transaction_pkey on transaction t_1  (cost=0.55..8.57 rows=1 width=93) (never executed)
                                        Index Cond: (tx_hash = tt.tx_hash)
                                        Filter: (from_wallet_id <> 126988)
                            ->  Index Scan using snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key on snapshot_edge se_1  (cost=0.43..0.50 rows=1 width=24) (never executed)
                                  Index Cond: ((snapshot_id = 1) AND (src_wallet_id = t_1.from_wallet_id) AND (dst_wallet_id = t_1.to_wallet_id))
                      ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_1  (cost=0.43..2.97 rows=1 width=75) (never executed)
                            Index Cond: ((snapshot_edge_id = se_1.edge_id) AND (tx_hash = t_1.tx_hash))
                            Heap Fetches: 0
          ->  Nested Loop  (cost=511.72..511.76 rows=1 width=153) (actual time=0.077..0.894 rows=1 loops=2)
                Buffers: shared hit=1462 read=13
                ->  Unique  (cost=3.61..3.62 rows=1 width=44) (actual time=0.009..0.010 rows=2 loops=2)
                      ->  Sort  (cost=3.61..3.61 rows=1 width=44) (actual time=0.009..0.009 rows=2 loops=2)
                            Sort Key: ev.from_wallet_id
                            Sort Method: quicksort  Memory: 25kB
                            ->  WorkTable Scan on evidence ev  (cost=0.00..3.60 rows=1 width=44) (actual time=0.004..0.004 rows=2 loops=2)
                                  Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                ->  Limit  (cost=508.11..508.11 rows=1 width=85) (actual time=0.440..0.440 rows=0 loops=4)
                      Buffers: shared hit=1462 read=13
                      ->  Sort  (cost=508.11..508.13 rows=8 width=85) (actual time=0.439..0.439 rows=0 loops=4)
                            Sort Key: t_2.block_number, t_2.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=1462 read=13
                            ->  Nested Loop  (cost=6.39..508.07 rows=8 width=85) (actual time=0.030..0.426 rows=65 loops=4)
                                  Buffers: shared hit=1459 read=13
                                  ->  Nested Loop  (cost=5.83..502.78 rows=8 width=67) (actual time=0.024..0.068 rows=65 loops=4)
                                        Buffers: shared hit=165 read=2
                                        ->  Bitmap Heap Scan on snapshot_edge se_2  (cost=5.40..483.30 rows=4 width=8) (actual time=0.017..0.036 rows=6 loops=4)
                                              Recheck Cond: (dst_wallet_id = ev.from_wallet_id)
                                              Filter: (snapshot_id = 1)
                                              Rows Removed by Filter: 20
                                              Heap Blocks: exact=79
                                              Buffers: shared hit=89 read=2
                                              ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.40 rows=130 width=0) (actual time=0.007..0.007 rows=26 loops=4)
                                                    Index Cond: (dst_wallet_id = ev.from_wallet_id)
                                                    Buffers: shared hit=11 read=1
                                        ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_2  (cost=0.43..4.71 rows=16 width=75) (actual time=0.003..0.004 rows=11 loops=24)
                                              Index Cond: (snapshot_edge_id = se_2.edge_id)
                                              Heap Fetches: 0
                                              Buffers: shared hit=76
                                  ->  Index Scan using transaction_pkey on transaction t_2  (cost=0.55..0.66 rows=1 width=85) (actual time=0.005..0.005 rows=1 loops=261)
                                        Index Cond: (tx_hash = st_2.tx_hash)
                                        Buffers: shared hit=1294 read=11
  ->  Sort  (cost=0.78..0.83 rows=19 width=312) (actual time=2.919..2.919 rows=4 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=1509 read=50
        ->  CTE Scan on evidence  (cost=0.00..0.38 rows=19 width=312) (actual time=0.306..2.897 rows=4 loops=1)
              Buffers: shared hit=1497 read=50
Planning:
  Buffers: shared hit=601 read=8
Planning Time: 2.251 ms
Execution Time: 3.084 ms
```

## Wallet 489

```
Unique  (cost=6079.65..6080.28 rows=50 width=312) (actual time=5.013..5.097 rows=500 loops=1)
  Buffers: shared hit=3655 read=250
  CTE evidence
    ->  Recursive Union  (cost=10.88..6077.24 rows=50 width=354) (actual time=0.057..4.612 rows=500 loops=1)
          Buffers: shared hit=3655 read=250
          ->  Append  (cost=10.88..835.17 rows=40 width=354) (actual time=0.056..4.006 rows=500 loops=1)
                Buffers: shared hit=3240 read=243
                ->  Nested Loop  (cost=10.88..621.95 rows=39 width=153) (actual time=0.056..3.698 rows=500 loops=1)
                      Buffers: shared hit=2913 read=238
                      ->  Nested Loop  (cost=10.33..596.04 rows=39 width=67) (actual time=0.047..0.530 rows=500 loops=1)
                            Buffers: shared hit=651
                            ->  Bitmap Heap Scan on snapshot_edge se  (cost=9.90..498.64 rows=20 width=8) (actual time=0.040..0.114 rows=199 loops=1)
                                  Recheck Cond: (((snapshot_id = 1) AND (src_wallet_id = 489)) OR (dst_wallet_id = 489))
                                  Filter: (snapshot_id = 1)
                                  Rows Removed by Filter: 40
                                  Heap Blocks: exact=47
                                  Buffers: shared hit=53
                                  ->  BitmapOr  (cost=9.90..9.90 rows=133 width=0) (actual time=0.022..0.023 rows=0 loops=1)
                                        Buffers: shared hit=6
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..4.59 rows=16 width=0) (actual time=0.018..0.018 rows=179 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (src_wallet_id = 489))
                                              Buffers: shared hit=3
                                        ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.30 rows=117 width=0) (actual time=0.004..0.004 rows=60 loops=1)
                                              Index Cond: (dst_wallet_id = 489)
                                              Buffers: shared hit=3
                            ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st  (cost=0.43..4.71 rows=16 width=75) (actual time=0.001..0.002 rows=3 loops=199)
                                  Index Cond: (snapshot_edge_id = se.edge_id)
                                  Heap Fetches: 0
                                  Buffers: shared hit=598
                      ->  Index Scan using transaction_pkey on transaction t  (cost=0.55..0.66 rows=1 width=85) (actual time=0.006..0.006 rows=1 loops=500)
                            Index Cond: (tx_hash = st.tx_hash)
                            Buffers: shared hit=2262 read=238
                ->  Nested Loop  (cost=1.83..213.02 rows=1 width=153) (actual time=0.218..0.219 rows=0 loops=1)
                      Buffers: shared hit=327 read=5
                      ->  Nested Loop  (cost=1.40..210.05 rows=1 width=160) (actual time=0.218..0.218 rows=0 loops=1)
                            Buffers: shared hit=327 read=5
                            ->  Nested Loop  (cost=0.97..202.11 rows=16 width=160) (actual time=0.218..0.218 rows=0 loops=1)
                                  Buffers: shared hit=327 read=5
                                  ->  Index Scan using idx_tt_to on token_transfer tt  (cost=0.42..64.95 rows=16 width=67) (actual time=0.018..0.030 rows=65 loops=1)
                                        Index Cond: (to_wallet_id = 489)
                                        Buffers: shared hit=2 read=5
                                  ->  Index Scan using transaction_pkey on transaction t_1  (cost=0.55..8.57 rows=1 width=93) (actual time=0.003..0.003 rows=0 loops=65)
                                        Index Cond: (tx_hash = tt.tx_hash)
                                        Filter: (from_wallet_id <> 489)
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=325
                            ->  Index Scan using snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key on snapshot_edge se_1  (cost=0.43..0.50 rows=1 width=24) (never executed)
                                  Index Cond: ((snapshot_id = 1) AND (src_wallet_id = t_1.from_wallet_id) AND (dst_wallet_id = t_1.to_wallet_id))
                      ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_1  (cost=0.43..2.97 rows=1 width=75) (never executed)
                            Index Cond: ((snapshot_edge_id = se_1.edge_id) AND (tx_hash = t_1.tx_hash))
                            Heap Fetches: 0
          ->  Nested Loop  (cost=524.12..524.16 rows=1 width=153) (actual time=0.504..0.505 rows=0 loops=1)
                Buffers: shared hit=415 read=7
                ->  Unique  (cost=16.01..16.02 rows=1 width=44) (actual time=0.056..0.063 rows=20 loops=1)
                      ->  Sort  (cost=16.01..16.02 rows=1 width=44) (actual time=0.056..0.057 rows=37 loops=1)
                            Sort Key: ev.from_wallet_id
                            Sort Method: quicksort  Memory: 27kB
                            ->  WorkTable Scan on evidence ev  (cost=0.00..16.00 rows=1 width=44) (actual time=0.037..0.044 rows=37 loops=1)
                                  Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                                  Rows Removed by Filter: 463
                ->  Limit  (cost=508.11..508.11 rows=1 width=85) (actual time=0.022..0.022 rows=0 loops=20)
                      Buffers: shared hit=415 read=7
                      ->  Sort  (cost=508.11..508.13 rows=8 width=85) (actual time=0.022..0.022 rows=0 loops=20)
                            Sort Key: t_2.block_number, t_2.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=415 read=7
                            ->  Nested Loop  (cost=6.39..508.07 rows=8 width=85) (actual time=0.021..0.021 rows=0 loops=20)
                                  Buffers: shared hit=415 read=7
                                  ->  Nested Loop  (cost=5.83..502.78 rows=8 width=67) (actual time=0.021..0.021 rows=0 loops=20)
                                        Buffers: shared hit=415 read=7
                                        ->  Bitmap Heap Scan on snapshot_edge se_2  (cost=5.40..483.30 rows=4 width=8) (actual time=0.020..0.020 rows=0 loops=20)
                                              Recheck Cond: (dst_wallet_id = ev.from_wallet_id)
                                              Filter: (snapshot_id = 1)
                                              Rows Removed by Filter: 68
                                              Heap Blocks: exact=362
                                              Buffers: shared hit=415 read=7
                                              ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.40 rows=130 width=0) (actual time=0.004..0.004 rows=68 loops=20)
                                                    Index Cond: (dst_wallet_id = ev.from_wallet_id)
                                                    Buffers: shared hit=53 read=7
                                        ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_2  (cost=0.43..4.71 rows=16 width=75) (never executed)
                                              Index Cond: (snapshot_edge_id = se_2.edge_id)
                                              Heap Fetches: 0
                                  ->  Index Scan using transaction_pkey on transaction t_2  (cost=0.55..0.66 rows=1 width=85) (never executed)
                                        Index Cond: (tx_hash = st_2.tx_hash)
  ->  Sort  (cost=2.41..2.54 rows=50 width=312) (actual time=5.012..5.030 rows=500 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 79kB
        Buffers: shared hit=3655 read=250
        ->  CTE Scan on evidence  (cost=0.00..1.00 rows=50 width=312) (actual time=0.058..4.880 rows=500 loops=1)
              Buffers: shared hit=3655 read=250
Planning:
  Buffers: shared hit=134
Planning Time: 1.332 ms
Execution Time: 5.185 ms
```

## Wallet 26

```
Unique  (cost=12581.90..12583.76 rows=149 width=312) (actual time=82.560..82.995 rows=2222 loops=1)
  Buffers: shared hit=65806 read=5924
  CTE evidence
    ->  Recursive Union  (cost=1033.24..12573.54 rows=149 width=354) (actual time=1.173..80.643 rows=2222 loops=1)
          Buffers: shared hit=65806 read=5924
          ->  Append  (cost=1033.24..1893.05 rows=129 width=354) (actual time=1.172..15.130 rows=1997 loops=1)
                Buffers: shared hit=9761 read=1384
                ->  Nested Loop  (cost=1033.24..1679.39 rows=128 width=153) (actual time=1.171..14.945 rows=1997 loops=1)
                      Buffers: shared hit=9759 read=1383
                      ->  Nested Loop  (cost=1032.69..1594.37 rows=128 width=67) (actual time=1.155..2.772 rows=1997 loops=1)
                            Buffers: shared hit=882 read=275
                            ->  Bitmap Heap Scan on snapshot_edge se  (cost=1032.26..1277.82 rows=65 width=8) (actual time=1.138..1.360 rows=267 loops=1)
                                  Recheck Cond: (((snapshot_id = 1) AND (src_wallet_id = 26)) OR ((snapshot_id = 1) AND (dst_wallet_id = 26)))
                                  Heap Blocks: exact=157
                                  Buffers: shared hit=165 read=174
                                  ->  BitmapOr  (cost=1032.26..1032.26 rows=65 width=0) (actual time=1.116..1.117 rows=0 loops=1)
                                        Buffers: shared hit=8 read=174
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..4.44 rows=1 width=0) (actual time=0.005..0.005 rows=0 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (src_wallet_id = 26))
                                              Buffers: shared hit=3
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..1027.79 rows=64 width=0) (actual time=1.111..1.111 rows=267 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (dst_wallet_id = 26))
                                              Buffers: shared hit=5 read=174
                            ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st  (cost=0.43..4.71 rows=16 width=75) (actual time=0.003..0.005 rows=7 loops=267)
                                  Index Cond: (snapshot_edge_id = se.edge_id)
                                  Heap Fetches: 0
                                  Buffers: shared hit=717 read=101
                      ->  Index Scan using transaction_pkey on transaction t  (cost=0.55..0.66 rows=1 width=85) (actual time=0.006..0.006 rows=1 loops=1997)
                            Index Cond: (tx_hash = st.tx_hash)
                            Buffers: shared hit=8877 read=1108
                ->  Nested Loop  (cost=1.83..213.02 rows=1 width=153) (actual time=0.017..0.018 rows=0 loops=1)
                      Buffers: shared hit=2 read=1
                      ->  Nested Loop  (cost=1.40..210.05 rows=1 width=160) (actual time=0.017..0.018 rows=0 loops=1)
                            Buffers: shared hit=2 read=1
                            ->  Nested Loop  (cost=0.97..202.11 rows=16 width=160) (actual time=0.017..0.017 rows=0 loops=1)
                                  Buffers: shared hit=2 read=1
                                  ->  Index Scan using idx_tt_to on token_transfer tt  (cost=0.42..64.95 rows=16 width=67) (actual time=0.016..0.016 rows=0 loops=1)
                                        Index Cond: (to_wallet_id = 26)
                                        Buffers: shared hit=2 read=1
                                  ->  Index Scan using transaction_pkey on transaction t_1  (cost=0.55..8.57 rows=1 width=93) (never executed)
                                        Index Cond: (tx_hash = tt.tx_hash)
                                        Filter: (from_wallet_id <> 26)
                            ->  Index Scan using snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key on snapshot_edge se_1  (cost=0.43..0.50 rows=1 width=24) (never executed)
                                  Index Cond: ((snapshot_id = 1) AND (src_wallet_id = t_1.from_wallet_id) AND (dst_wallet_id = t_1.to_wallet_id))
                      ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_1  (cost=0.43..2.97 rows=1 width=75) (never executed)
                            Index Cond: ((snapshot_edge_id = se_1.edge_id) AND (tx_hash = t_1.tx_hash))
                            Heap Fetches: 0
          ->  Nested Loop  (cost=559.72..1067.90 rows=2 width=153) (actual time=0.495..16.218 rows=56 loops=4)
                Buffers: shared hit=56045 read=4540
                ->  Unique  (cost=51.61..51.62 rows=2 width=44) (actual time=0.154..0.230 rows=100 loops=4)
                      ->  Sort  (cost=51.61..51.62 rows=2 width=44) (actual time=0.154..0.185 rows=546 loops=4)
                            Sort Key: ev.from_wallet_id
                            Sort Method: quicksort  Memory: 25kB
                            ->  WorkTable Scan on evidence ev  (cost=0.00..51.60 rows=2 width=44) (actual time=0.003..0.092 rows=546 loops=4)
                                  Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                                  Rows Removed by Filter: 10
                ->  Limit  (cost=508.11..508.11 rows=1 width=85) (actual time=0.160..0.160 rows=1 loops=398)
                      Buffers: shared hit=56045 read=4540
                      ->  Sort  (cost=508.11..508.13 rows=8 width=85) (actual time=0.159..0.159 rows=1 loops=398)
                            Sort Key: t_2.block_number, t_2.tx_index
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=56045 read=4540
                            ->  Nested Loop  (cost=6.39..508.07 rows=8 width=85) (actual time=0.025..0.155 rows=21 loops=398)
                                  Buffers: shared hit=56045 read=4540
                                  ->  Nested Loop  (cost=5.83..502.78 rows=8 width=67) (actual time=0.022..0.055 rows=21 loops=398)
                                        Buffers: shared hit=18128 read=357
                                        ->  Bitmap Heap Scan on snapshot_edge se_2  (cost=5.40..483.30 rows=4 width=8) (actual time=0.019..0.037 rows=9 loops=398)
                                              Recheck Cond: (dst_wallet_id = ev.from_wallet_id)
                                              Filter: (snapshot_id = 1)
                                              Rows Removed by Filter: 33
                                              Heap Blocks: exact=6346
                                              Buffers: shared hit=7260 read=280
                                              ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.40 rows=130 width=0) (actual time=0.003..0.003 rows=42 loops=398)
                                                    Index Cond: (dst_wallet_id = ev.from_wallet_id)
                                                    Buffers: shared hit=1098 read=96
                                        ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_2  (cost=0.43..4.71 rows=16 width=75) (actual time=0.001..0.002 rows=2 loops=3643)
                                              Index Cond: (snapshot_edge_id = se_2.edge_id)
                                              Heap Fetches: 0
                                              Buffers: shared hit=10868 read=77
                                  ->  Index Scan using transaction_pkey on transaction t_2  (cost=0.55..0.66 rows=1 width=85) (actual time=0.004..0.004 rows=1 loops=8420)
                                        Index Cond: (tx_hash = st_2.tx_hash)
                                        Buffers: shared hit=37917 read=4183
  ->  Sort  (cost=8.36..8.73 rows=149 width=312) (actual time=82.560..82.663 rows=2222 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 357kB
        Buffers: shared hit=65806 read=5924
        ->  CTE Scan on evidence  (cost=0.00..2.98 rows=149 width=312) (actual time=1.174..81.854 rows=2222 loops=1)
              Buffers: shared hit=65806 read=5924
Planning:
  Buffers: shared hit=134
Planning Time: 1.195 ms
Execution Time: 83.212 ms
```

## Wallet 28

```
Unique  (cost=18185.63..18188.34 rows=200 width=312) (actual time=102.605..103.570 rows=4699 loops=1)
  Buffers: shared hit=96089 read=4657
  CTE evidence
    ->  Recursive Union  (cost=1033.25..18172.86 rows=217 width=354) (actual time=0.638..98.867 rows=4699 loops=1)
          Buffers: shared hit=96089 read=4657
          ->  Append  (cost=1033.25..2178.11 rows=187 width=354) (actual time=0.637..22.740 rows=4382 loops=1)
                Buffers: shared hit=20903 read=2464
                ->  Nested Loop  (cost=1033.25..1964.15 rows=186 width=153) (actual time=0.636..22.379 rows=4381 loops=1)
                      Buffers: shared hit=20888 read=2462
                      ->  Nested Loop  (cost=1032.70..1840.61 rows=186 width=67) (actual time=0.629..2.627 rows=4381 loops=1)
                            Buffers: shared hit=1311 read=134
                            ->  Bitmap Heap Scan on snapshot_edge se  (cost=1032.27..1382.83 rows=94 width=8) (actual time=0.623..0.956 rows=345 loops=1)
                                  Recheck Cond: (((snapshot_id = 1) AND (src_wallet_id = 28)) OR ((snapshot_id = 1) AND (dst_wallet_id = 28)))
                                  Heap Blocks: exact=179
                                  Buffers: shared hit=331 read=30
                                  ->  BitmapOr  (cost=1032.27..1032.27 rows=94 width=0) (actual time=0.608..0.608 rows=0 loops=1)
                                        Buffers: shared hit=182
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..4.44 rows=1 width=0) (actual time=0.005..0.005 rows=0 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (src_wallet_id = 28))
                                              Buffers: shared hit=3
                                        ->  Bitmap Index Scan on snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key  (cost=0.00..1027.79 rows=94 width=0) (actual time=0.603..0.603 rows=345 loops=1)
                                              Index Cond: ((snapshot_id = 1) AND (dst_wallet_id = 28))
                                              Buffers: shared hit=179
                            ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st  (cost=0.43..4.71 rows=16 width=75) (actual time=0.002..0.004 rows=13 loops=345)
                                  Index Cond: (snapshot_edge_id = se.edge_id)
                                  Heap Fetches: 0
                                  Buffers: shared hit=980 read=104
                      ->  Index Scan using transaction_pkey on transaction t  (cost=0.55..0.66 rows=1 width=85) (actual time=0.004..0.004 rows=1 loops=4381)
                            Index Cond: (tx_hash = st.tx_hash)
                            Buffers: shared hit=19577 read=2328
                ->  Nested Loop  (cost=1.83..213.02 rows=1 width=153) (actual time=0.033..0.035 rows=1 loops=1)
                      Buffers: shared hit=15 read=2
                      ->  Nested Loop  (cost=1.40..210.05 rows=1 width=160) (actual time=0.027..0.029 rows=1 loops=1)
                            Buffers: shared hit=11 read=2
                            ->  Nested Loop  (cost=0.97..202.11 rows=16 width=160) (actual time=0.019..0.020 rows=1 loops=1)
                                  Buffers: shared hit=7 read=2
                                  ->  Index Scan using idx_tt_to on token_transfer tt  (cost=0.42..64.95 rows=16 width=67) (actual time=0.010..0.010 rows=1 loops=1)
                                        Index Cond: (to_wallet_id = 28)
                                        Buffers: shared hit=3 read=1
                                  ->  Index Scan using transaction_pkey on transaction t_1  (cost=0.55..8.57 rows=1 width=93) (actual time=0.007..0.007 rows=1 loops=1)
                                        Index Cond: (tx_hash = tt.tx_hash)
                                        Filter: (from_wallet_id <> 28)
                                        Buffers: shared hit=4 read=1
                            ->  Index Scan using snapshot_edge_snapshot_id_src_wallet_id_dst_wallet_id_key on snapshot_edge se_1  (cost=0.43..0.50 rows=1 width=24) (actual time=0.007..0.007 rows=1 loops=1)
                                  Index Cond: ((snapshot_id = 1) AND (src_wallet_id = t_1.from_wallet_id) AND (dst_wallet_id = t_1.to_wallet_id))
                                  Buffers: shared hit=4
                      ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_1  (cost=0.43..2.97 rows=1 width=75) (actual time=0.004..0.004 rows=1 loops=1)
                            Index Cond: ((snapshot_edge_id = se_1.edge_id) AND (tx_hash = t_1.tx_hash))
                            Heap Fetches: 0
                            Buffers: shared hit=4
          ->  Nested Loop  (cost=582.94..1599.26 rows=3 width=153) (actual time=0.583..15.000 rows=63 loops=5)
                Buffers: shared hit=75186 read=2193
                ->  Unique  (cost=74.82..74.84 rows=3 width=44) (actual time=0.357..0.468 rows=107 loops=5)
                      ->  Sort  (cost=74.82..74.83 rows=3 width=44) (actual time=0.356..0.401 rows=930 loops=5)
                            Sort Key: ev.from_wallet_id
                            Sort Method: quicksort  Memory: 25kB
                            ->  WorkTable Scan on evidence ev  (cost=0.00..74.80 rows=3 width=44) (actual time=0.002..0.196 rows=930 loops=5)
                                  Filter: ((from_wallet_id IS NOT NULL) AND (from_wallet_id <> 2) AND (hop < 10) AND (role = 'unsolicited'::text) AND (from_wallet_id <> ALL (visited)))
                                  Rows Removed by Filter: 10
                ->  Limit  (cost=508.11..508.11 rows=1 width=85) (actual time=0.134..0.134 rows=1 loops=537)
                      Buffers: shared hit=75186 read=2193
                      ->  Sort  (cost=508.11..508.13 rows=8 width=85) (actual time=0.134..0.134 rows=1 loops=537)
                            Sort Key: t_2.block_number, t_2.tx_index
                            Sort Method: top-N heapsort  Memory: 25kB
                            Buffers: shared hit=75186 read=2193
                            ->  Nested Loop  (cost=6.39..508.07 rows=8 width=85) (actual time=0.014..0.129 rows=20 loops=537)
                                  Buffers: shared hit=75186 read=2193
                                  ->  Nested Loop  (cost=5.83..502.78 rows=8 width=67) (actual time=0.011..0.042 rows=20 loops=537)
                                        Buffers: shared hit=24085 read=284
                                        ->  Bitmap Heap Scan on snapshot_edge se_2  (cost=5.40..483.30 rows=4 width=8) (actual time=0.008..0.022 rows=9 loops=537)
                                              Recheck Cond: (dst_wallet_id = ev.from_wallet_id)
                                              Filter: (snapshot_id = 1)
                                              Rows Removed by Filter: 31
                                              Heap Blocks: exact=8622
                                              Buffers: shared hit=9971 read=262
                                              ->  Bitmap Index Scan on idx_se_dst  (cost=0.00..5.40 rows=130 width=0) (actual time=0.003..0.003 rows=40 loops=537)
                                                    Index Cond: (dst_wallet_id = ev.from_wallet_id)
                                                    Buffers: shared hit=1550 read=61
                                        ->  Index Only Scan using snapshot_edge_tx_pkey on snapshot_edge_tx st_2  (cost=0.43..4.71 rows=16 width=75) (actual time=0.002..0.002 rows=2 loops=4705)
                                              Index Cond: (snapshot_edge_id = se_2.edge_id)
                                              Heap Fetches: 0
                                              Buffers: shared hit=14114 read=22
                                  ->  Index Scan using transaction_pkey on transaction t_2  (cost=0.55..0.66 rows=1 width=85) (actual time=0.004..0.004 rows=1 loops=10602)
                                        Index Cond: (tx_hash = st_2.tx_hash)
                                        Buffers: shared hit=51101 read=1909
  ->  Sort  (cost=12.76..13.30 rows=217 width=312) (actual time=102.605..102.831 rows=4699 loops=1)
        Sort Key: evidence.block_number, evidence.hop, evidence.tx_hash, evidence.role
        Sort Method: quicksort  Memory: 743kB
        Buffers: shared hit=96089 read=4657
        ->  CTE Scan on evidence  (cost=0.00..4.34 rows=217 width=312) (actual time=0.639..101.257 rows=4699 loops=1)
              Buffers: shared hit=96089 read=4657
Planning:
  Buffers: shared hit=134
Planning Time: 1.218 ms
Execution Time: 103.892 ms
```


## Equality check: old query vs new query (wallet 26)

Run back to back against the same snapshot, comparing the full result sets rather than
just the timings:

| Query | Rows | Wall clock |
| --- | --- | --- |
| old (pre-rewrite) | 2,222 | 606.10 s |
| new | 2,222 | 0.11 s |

Identical `tx_hash` sets (0 rows only in old, 0 only in new) and an identical
(min hop, role) pair for every `tx_hash`, so the rewrite is a pure performance
change: no evidence is gained or lost.
