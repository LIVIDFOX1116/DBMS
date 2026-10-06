-- FrameCheck migration (2026-10): adds the audit-layer tables on top of the
-- existing schema.sql. Every statement is IF NOT EXISTS -- safe to re-run,
-- same idempotency guarantee schema.sql's own ADD COLUMN IF NOT EXISTS
-- migrations already rely on. Applied by db/init_db.py after schema.sql,
-- in filename order, from db/migrations/.
--
-- relationship to the existing `alert` table: `alert` already flags
-- plausible Sybil clusters (see run_analysis.py's classify_clusters()) for
-- an analyst to review. `flag` is a different, broader thing: one row per
-- detector-raised signal that FrameCheck's audit engine (graph/audit.py)
-- can interrogate for frameability. A `sybil_cluster` flag producer reads
-- the SAME classify_clusters() output that already feeds `alert` -- it
-- does not re-detect anything -- so `flag` augments `alert` with an
-- "is this evidence trustworthy" layer, it isn't a competing alert system.

BEGIN;

-- Lets the framing simulator (scripts/simulate_framing.py, Phase 5) mark
-- every synthetic transaction it inserts, so a simulated dust transfer is
-- never mistaken for a real one by any detector, query, or UI view.
ALTER TABLE transaction ADD COLUMN IF NOT EXISTS is_simulated BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS framing_scenario (
    scenario_id         BIGSERIAL PRIMARY KEY,
    attacker_wallet_ids BIGINT[] NOT NULL,
    target_wallet_ids   BIGINT[] NOT NULL,
    transfer_count      INT NOT NULL,
    total_value_wei     NUMERIC(38,0) NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS flag (
    flag_id    BIGSERIAL PRIMARY KEY,
    run_id     BIGINT NOT NULL REFERENCES analysis_run(run_id),
    wallet_id  BIGINT NOT NULL REFERENCES wallet(wallet_id),
    detector   VARCHAR(30) NOT NULL,
    score      DOUBLE PRECISION,
    threshold  DOUBLE PRECISION,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_flag_run    ON flag(run_id);
CREATE INDEX IF NOT EXISTS idx_flag_wallet ON flag(wallet_id);

-- One row per transaction a flag's audit trail rests on, time-ordered by
-- block_number -- role is set from transaction.from_wallet_id, never from
-- token_transfer.from_wallet_id (see CLAUDE.md's definitions section).
CREATE TABLE IF NOT EXISTS flag_evidence (
    flag_id      BIGINT NOT NULL REFERENCES flag(flag_id),
    tx_hash      CHAR(66) NOT NULL REFERENCES transaction(tx_hash),
    hop          INT NOT NULL,
    block_number BIGINT NOT NULL,
    role         VARCHAR(12) NOT NULL CHECK (role IN ('signed', 'unsolicited')),
    PRIMARY KEY (flag_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_flag_evidence_flag ON flag_evidence(flag_id);

-- Multiple audit rows per flag_id are allowed on purpose (not UNIQUE on
-- flag_id) -- same philosophy as analysis_run: a Monday audit and a Friday
-- audit (e.g. after a detector or method change) never overwrite each
-- other, each is its own timestamped row.
CREATE TABLE IF NOT EXISTS flag_audit (
    flag_audit_id    BIGSERIAL PRIMARY KEY,
    flag_id          BIGINT NOT NULL REFERENCES flag(flag_id),
    verdict          VARCHAR(12) NOT NULL CHECK (verdict IN ('frameable', 'robust')),
    framing_cost_wei NUMERIC(38,0),
    framing_gas      BIGINT,
    minimal_tx_set   JSONB,
    method           VARCHAR(20) NOT NULL CHECK (method IN ('exact', 'greedy_upper_bound', 'yes_no')),
    backend          VARCHAR(40),
    runtime_ms       INT,
    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_flag_audit_flag ON flag_audit(flag_id);

-- Evidence-chain query support: flag_evidence's recursive CTE (Phase 4,
-- db/queries/evidence.sql) walks transaction history toward the seed,
-- ordered by block_number, filtered by to_wallet_id or from_wallet_id --
-- the existing idx_tx_to/idx_tx_from (schema.sql) cover the filter alone,
-- these add block_number so the walk's ORDER BY is index-backed too.
CREATE INDEX IF NOT EXISTS idx_tx_to_block   ON transaction(to_wallet_id, block_number);
CREATE INDEX IF NOT EXISTS idx_tx_from_block ON transaction(from_wallet_id, block_number);

-- BRIN, not B-tree: block_number is naturally ordered/append-mostly (new
-- blocks only ever increase), which is exactly what BRIN is for -- a
-- fraction of a B-tree's size for a column like this.
CREATE INDEX IF NOT EXISTS idx_tx_block_brin ON transaction USING BRIN(block_number);

COMMIT;
