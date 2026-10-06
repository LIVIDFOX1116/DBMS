-- FrameCheck migration (2026-10): extends the impurity/engaged_proximity
-- haircut replay to ERC-20 token_transfer rows, tracked per token
-- contract independently of native ETH. "A flag fires if any token
-- crosses the threshold" (CLAUDE.md) -- this column records which
-- currency triggered it: NULL for native ETH (and for every
-- bfs_proximity/sybil_cluster/pagerank_threshold flag, which never has a
-- currency dimension at all), or an ERC-20 contract_address for a flag
-- raised because that specific token's ratio crossed the threshold.

BEGIN;

ALTER TABLE flag ADD COLUMN IF NOT EXISTS token_contract CHAR(42);

COMMIT;
