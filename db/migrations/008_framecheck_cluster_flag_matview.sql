-- FrameCheck migration (2026-10, Phase 10): precomputed cluster flag counts.
--
-- /clusters/top-flagged joined 780,644 cluster_member rows to 128,637 flag
-- rows and grouped with COUNT(DISTINCT) on every call: 471 ms measured.
-- This view does that join once; the endpoint then aggregates 5,532 small
-- rows instead, which measured 2 ms.
--
-- Grain is deliberately (cluster_run, flag_run, cluster, WALLET), not a
-- pre-aggregated count per cluster. A first attempt stored
-- count(DISTINCT wallet_id) per flag_run and summed it at query time, which
-- reported 26,761 flagged wallets for cluster 351299 where the live query
-- says 26,594 -- summing per-run distinct counts double-counts any wallet
-- flagged by two detectors. Distinct counting has to happen across the
-- selected runs together, so the wallet has to survive into the view.
--
-- Audit verdicts are deliberately NOT here. They change every time a single
-- flag is audited, which would mean refreshing this whole view to record one
-- verdict; and there are only a few hundred audit rows, so joining them live
-- for the eight clusters that survive the ranking is nearly free.

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS mv_cluster_flag_stats;

CREATE MATERIALIZED VIEW mv_cluster_flag_stats AS
SELECT c.run_id          AS cluster_run_id,
       f.run_id          AS flag_run_id,
       c.cluster_id,
       c.community_label,
       c.member_count,
       cm.wallet_id,
       count(*)          AS flags_for_wallet
FROM cluster c
JOIN cluster_member cm ON cm.cluster_id = c.cluster_id
JOIN flag f            ON f.wallet_id   = cm.wallet_id
GROUP BY c.run_id, f.run_id, c.cluster_id, c.community_label,
         c.member_count, cm.wallet_id;

-- The endpoint always filters by cluster_run_id then flag_run_id.
CREATE INDEX IF NOT EXISTS idx_mv_cluster_flag_stats_lookup
    ON mv_cluster_flag_stats (cluster_run_id, flag_run_id, cluster_id);

-- REFRESH ... CONCURRENTLY needs a unique index, and being able to refresh
-- without blocking readers matters: a refresh runs right after a detector
-- job, while someone may well be looking at the dashboard.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_cluster_flag_stats_key
    ON mv_cluster_flag_stats (cluster_run_id, flag_run_id, cluster_id, wallet_id);

COMMIT;

ANALYZE mv_cluster_flag_stats;
