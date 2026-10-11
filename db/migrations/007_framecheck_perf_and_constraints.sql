-- FrameCheck migration (2026-10, Phase 10): indexes and constraints driven
-- by measurement, not guesswork. Every index here was added because an
-- EXPLAIN (ANALYZE, BUFFERS) in docs/perf/db_baseline.md showed the planner
-- doing something expensive without it; nothing is added speculatively.
--
-- Accuracy note deliberately NOT acted on here: snapshot_edge.edge_weight is
-- DOUBLE PRECISION holding wei, which rounds beyond ~15 significant digits.
-- Measured on snapshot 2: 8.6% of value-carrying edges differ from the exact
-- NUMERIC sum, but the largest absolute error is 30,000,000 wei
-- (0.00000003 ETH, relative 5e-15). Widening the column would rewrite a
-- 174 MB table and perturb the PageRank/Louvain inputs that produce flags --
-- a real risk to the before/after regression guard -- to recover an error
-- far below anything that changes a ranking. Framing costs never read this
-- column (they read transaction.value_wei, a NUMERIC). Documented in
-- docs/perf/db_after.md rather than changed.

BEGIN;

-- /clusters ordered by run_id was a Seq Scan over 516,622 rows feeding a
-- Materialize that the nested loop re-read 17 times: 1,158 ms for 100 rows.
CREATE INDEX IF NOT EXISTS idx_cluster_run_id ON cluster (run_id DESC);

-- cluster_member's primary key is (cluster_id, wallet_id), which serves
-- "members of this cluster" but not the reverse lookup. /clusters/top-flagged
-- joins flagged wallets TO their clusters, i.e. by wallet_id.
CREATE INDEX IF NOT EXISTS idx_cluster_member_wallet ON cluster_member (wallet_id);

-- /wallets orders by tx_count and takes the top N; without this the planner
-- sorts all 610,055 rows (top-N heapsort over 12,641 heap blocks read).
-- NULLS LAST matches the query's own ordering, so the index can be walked
-- instead of sorted.
CREATE INDEX IF NOT EXISTS idx_wallet_tx_count ON wallet (tx_count DESC NULLS LAST);

-- Accuracy guards. Wei is unsigned on-chain, so a negative value is always a
-- parse or sign error rather than real data -- and a negative framing cost
-- would make an attack look free. Verified against the live data before
-- adding (scripts/check_data_quality.py reports zero violations), so these
-- validate without rewriting anything.
ALTER TABLE transaction  DROP CONSTRAINT IF EXISTS transaction_value_wei_nonneg;
ALTER TABLE transaction  ADD  CONSTRAINT transaction_value_wei_nonneg CHECK (value_wei >= 0);

ALTER TABLE token_transfer DROP CONSTRAINT IF EXISTS token_transfer_amount_nonneg;
ALTER TABLE token_transfer ADD  CONSTRAINT token_transfer_amount_nonneg CHECK (token_amount >= 0);

ALTER TABLE flag_audit DROP CONSTRAINT IF EXISTS flag_audit_cost_nonneg;
ALTER TABLE flag_audit ADD  CONSTRAINT flag_audit_cost_nonneg
    CHECK (framing_cost_wei IS NULL OR framing_cost_wei >= 0);

ALTER TABLE flag_audit DROP CONSTRAINT IF EXISTS flag_audit_gas_nonneg;
ALTER TABLE flag_audit ADD  CONSTRAINT flag_audit_gas_nonneg
    CHECK (framing_gas IS NULL OR framing_gas >= 0);

-- A self-loop CHECK (src_wallet_id <> dst_wallet_id) was drafted here and
-- deliberately NOT added: the data has 1,557 of them (523 on snapshot 2),
-- and they are legitimate -- real transactions where from == to, value 0,
-- i.e. contract self-calls. Constraining them away would reject correct
-- on-chain data. scripts/check_data_quality.py reports them as a warning
-- instead, because they do still sit in the PageRank/Louvain input.

COMMIT;

-- Outside the transaction: ANALYZE cannot run inside one usefully here, and
-- the new indexes need statistics before the planner will choose them.
ANALYZE cluster;
ANALYZE cluster_member;
ANALYZE wallet;
