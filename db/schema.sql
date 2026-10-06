-- GPU-Accelerated Ethereum Transaction-Graph Analyzer
-- PostgreSQL schema (v2 - corrected). See docs/ER_Diagram_TransactionGraphAnalyzer.svg
-- and docs/schema_v2.dbml for the full entity-relationship design and rationale.
--
-- Corrections vs the original v1 sketch:
--   1. wallet<->token_transfer relationships now have real FKs (were missing).
--   2. wallet<->snapshot_edge relationships now have real FKs (were missing).
--   3. token.contract_wallet_id is NOT NULL: it's a subtype link (the contract
--      IS a wallet row), not a "deployer" relationship - there is no deployer FK.
--   4. block.proposer_wallet_id replaces "miner_wallet_id": Ethereum has been
--      proof-of-stake since Sept 2022, so "miner" is legacy terminology.
--   5. snapshot_edge_tx bridge table added so one aggregated CSR edge can trace
--      back to every underlying transaction that fed it.

BEGIN;

CREATE TABLE IF NOT EXISTS analyst (
    analyst_id      SERIAL PRIMARY KEY,
    username        VARCHAR(40)  NOT NULL UNIQUE,
    email           VARCHAR(120) NOT NULL,
    password_hash   CHAR(60)     NOT NULL,
    role            VARCHAR(20)  NOT NULL DEFAULT 'analyst',
    organisation    VARCHAR(80),
    created_at      TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet (
    wallet_id        BIGSERIAL PRIMARY KEY,
    address          CHAR(42) NOT NULL UNIQUE,
    address_type     VARCHAR(10) NOT NULL DEFAULT 'EOA' CHECK (address_type IN ('EOA', 'CONTRACT')),
    first_seen_block BIGINT,
    last_seen_block  BIGINT,
    balance_wei      NUMERIC(38,0) DEFAULT 0,
    total_sent_wei   NUMERIC(38,0) DEFAULT 0,
    total_recv_wei   NUMERIC(38,0) DEFAULT 0,
    tx_count         INT DEFAULT 0,
    risk_score       DECIMAL(5,2)
);

CREATE TABLE IF NOT EXISTS block (
    block_number       BIGINT PRIMARY KEY,
    block_hash         CHAR(66) NOT NULL UNIQUE,
    proposer_wallet_id BIGINT REFERENCES wallet(wallet_id),
    block_timestamp    TIMESTAMP NOT NULL,
    gas_used           BIGINT,
    gas_limit          BIGINT,
    tx_count           INT
);

CREATE TABLE IF NOT EXISTS transaction (
    tx_hash        CHAR(66) PRIMARY KEY,
    block_number   BIGINT NOT NULL REFERENCES block(block_number),
    from_wallet_id BIGINT NOT NULL REFERENCES wallet(wallet_id),
    to_wallet_id   BIGINT REFERENCES wallet(wallet_id),  -- nullable: contract-creation txs have no recipient
    value_wei      NUMERIC(38,0) NOT NULL,
    gas_price      BIGINT,
    gas_used       BIGINT,
    tx_index       SMALLINT,
    nonce          INT,
    status         BOOLEAN,
    method_id      CHAR(10)
);
CREATE INDEX IF NOT EXISTS idx_tx_from ON transaction(from_wallet_id);
CREATE INDEX IF NOT EXISTS idx_tx_to   ON transaction(to_wallet_id);
CREATE INDEX IF NOT EXISTS idx_tx_block ON transaction(block_number);

CREATE TABLE IF NOT EXISTS token (
    token_id           SERIAL PRIMARY KEY,
    contract_wallet_id BIGINT NOT NULL UNIQUE REFERENCES wallet(wallet_id),
    contract_address   CHAR(42) NOT NULL UNIQUE,
    -- symbol/token_name are attacker-controlled: any contract can set an
    -- arbitrary ERC20 `symbol()`/`name()`, and phishing/spam tokens
    -- unsolicited-airdropped to well-known addresses routinely embed a full
    -- URL or message there. VARCHAR(16) crashed the very first live crawl
    -- (StringDataRightTruncation) against a real high-profile address.
    symbol             VARCHAR(128),
    token_name         VARCHAR(255),
    decimals           SMALLINT,
    standard           VARCHAR(10) DEFAULT 'ERC20'
);

CREATE TABLE IF NOT EXISTS token_transfer (
    transfer_id    BIGSERIAL PRIMARY KEY,
    tx_hash        CHAR(66) NOT NULL REFERENCES transaction(tx_hash),
    token_id       INT NOT NULL REFERENCES token(token_id),
    from_wallet_id BIGINT NOT NULL REFERENCES wallet(wallet_id),
    to_wallet_id   BIGINT NOT NULL REFERENCES wallet(wallet_id),
    -- An ERC20 Transfer event's value is a raw uint256 (max ~1.1579e77,
    -- 78 digits) -- unlike wei amounts, it's not bounded by any real supply,
    -- since spam/scam tokens routinely mint absurd raw amounts on purpose.
    -- NUMERIC(38,0) overflowed on the very first live crawl.
    token_amount   NUMERIC(78,0),
    log_index      SMALLINT,
    UNIQUE (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_tt_from ON token_transfer(from_wallet_id);
CREATE INDEX IF NOT EXISTS idx_tt_to   ON token_transfer(to_wallet_id);

CREATE TABLE IF NOT EXISTS label (
    label_id      SERIAL PRIMARY KEY,
    label_name    VARCHAR(60) NOT NULL UNIQUE,
    category      VARCHAR(30),
    source        VARCHAR(40),
    is_sanctioned BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS wallet_label (
    wallet_id  BIGINT NOT NULL REFERENCES wallet(wallet_id),
    label_id   INT    NOT NULL REFERENCES label(label_id),
    tagged_by  INT REFERENCES analyst(analyst_id),
    confidence DECIMAL(4,3),
    tagged_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (wallet_id, label_id)
);

CREATE TABLE IF NOT EXISTS graph_snapshot (
    snapshot_id     SERIAL PRIMARY KEY,
    snapshot_name   VARCHAR(80) NOT NULL UNIQUE,
    file_path       VARCHAR(255) NOT NULL,
    file_format     VARCHAR(10) NOT NULL DEFAULT 'parquet',
    start_block     BIGINT,
    end_block       BIGINT,
    vertex_count    BIGINT,
    edge_count      BIGINT,
    is_directed     BOOLEAN DEFAULT TRUE,
    checksum_sha256 CHAR(64),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS snapshot_edge (
    edge_id        BIGSERIAL PRIMARY KEY,
    snapshot_id    INT NOT NULL REFERENCES graph_snapshot(snapshot_id),
    src_wallet_id  BIGINT NOT NULL REFERENCES wallet(wallet_id),
    dst_wallet_id  BIGINT NOT NULL REFERENCES wallet(wallet_id),
    edge_weight    DOUBLE PRECISION,
    transfer_count INT DEFAULT 1,
    UNIQUE (snapshot_id, src_wallet_id, dst_wallet_id)
);
CREATE INDEX IF NOT EXISTS idx_se_src ON snapshot_edge(src_wallet_id);
CREATE INDEX IF NOT EXISTS idx_se_dst ON snapshot_edge(dst_wallet_id);

-- Bridge entity (new in v2): lets one aggregated CSR edge trace back to every
-- underlying transaction that fed it, since snapshot_edge aggregates by wallet
-- pair, not by individual transaction.
CREATE TABLE IF NOT EXISTS snapshot_edge_tx (
    snapshot_edge_id BIGINT NOT NULL REFERENCES snapshot_edge(edge_id),
    tx_hash          CHAR(66) NOT NULL REFERENCES transaction(tx_hash),
    PRIMARY KEY (snapshot_edge_id, tx_hash)
);

CREATE TABLE IF NOT EXISTS algorithm (
    algorithm_id     SERIAL PRIMARY KEY,
    algorithm_name   VARCHAR(40) NOT NULL UNIQUE,
    category         VARCHAR(20) NOT NULL,
    cugraph_function VARCHAR(60),
    default_params   JSONB,
    library_version  VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS analysis_run (
    run_id        BIGSERIAL PRIMARY KEY,
    snapshot_id   INT NOT NULL REFERENCES graph_snapshot(snapshot_id),
    algorithm_id  INT NOT NULL REFERENCES algorithm(algorithm_id),
    analyst_id    INT NOT NULL REFERENCES analyst(analyst_id),
    parameters    JSONB,
    gpu_device    VARCHAR(40),
    gpu_memory_mb INT,
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP,
    runtime_ms    INT,
    status        VARCHAR(12) DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS vertex_metric (
    metric_id     BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES analysis_run(run_id),
    wallet_id     BIGINT NOT NULL REFERENCES wallet(wallet_id),
    metric_name   VARCHAR(30) NOT NULL,
    metric_value  DOUBLE PRECISION,
    rank_position INT,
    UNIQUE (run_id, wallet_id, metric_name)
);
CREATE INDEX IF NOT EXISTS idx_vm_run ON vertex_metric(run_id);

CREATE TABLE IF NOT EXISTS cluster (
    cluster_id       BIGSERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES analysis_run(run_id),
    community_label  INT NOT NULL,
    member_count     INT,
    internal_edges   BIGINT,
    modularity_score DOUBLE PRECISION,
    cluster_type     VARCHAR(20),  -- SYBIL / WASH_TRADE / MIXER / PEEL_CHAIN
    risk_score       DECIMAL(5,2)
);

CREATE TABLE IF NOT EXISTS cluster_member (
    cluster_id       BIGINT NOT NULL REFERENCES cluster(cluster_id),
    wallet_id        BIGINT NOT NULL REFERENCES wallet(wallet_id),
    membership_score DOUBLE PRECISION,
    is_core_node     BOOLEAN DEFAULT FALSE,
    in_degree        INT,
    out_degree       INT,
    PRIMARY KEY (cluster_id, wallet_id)
);

CREATE TABLE IF NOT EXISTS alert (
    alert_id      BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES analysis_run(run_id),
    cluster_id    BIGINT REFERENCES cluster(cluster_id),
    wallet_id     BIGINT REFERENCES wallet(wallet_id),
    reviewed_by   INT REFERENCES analyst(analyst_id),
    pattern_type  VARCHAR(20) NOT NULL,
    severity      VARCHAR(10) NOT NULL,
    confidence    DECIMAL(4,3),
    detected_at   TIMESTAMP DEFAULT NOW(),
    review_status VARCHAR(12) DEFAULT 'NEW'
);

CREATE TABLE IF NOT EXISTS visualization (
    viz_id           SERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES analysis_run(run_id),
    layout_algorithm VARCHAR(40),
    image_path       VARCHAR(255),
    image_format     VARCHAR(10),
    nodes_rendered   INT,
    edges_rendered   INT,
    generated_at     TIMESTAMP DEFAULT NOW()
);

-- Seed the three algorithms this project targets (Objective 4 in the bible).
INSERT INTO algorithm (algorithm_name, category, cugraph_function, library_version)
VALUES
    ('pagerank', 'centrality', 'cugraph.pagerank', 'TBD'),
    ('louvain',  'community',  'cugraph.louvain',  'TBD'),
    ('bfs',      'traversal',  'cugraph.bfs',       'TBD')
ON CONFLICT (algorithm_name) DO NOTHING;

-- Seed a default analyst: analysis_run.analyst_id is NOT NULL and FK's here,
-- but nothing else in the pipeline creates analyst rows (no auth in this
-- project -- see API_CONTRACT.md's known simplifications). Without this,
-- graph/run_analysis.py's default analyst_id=1 has nothing to point at.
INSERT INTO analyst (username, email, password_hash, role, organisation)
VALUES ('system', 'system@local.dev', 'unused-no-auth-in-this-project---------------------------', 'admin', 'framecheck')
ON CONFLICT (username) DO NOTHING;

-- Migration (2026-09): per-run graph library version, alongside the
-- existing gpu_device (which stores backend_name -- e.g. "pandas+networkx
-- (CPU)" -- see graph/backend.py). Distinct from the unrelated, unused
-- algorithm.library_version column above (a static 'TBD' placeholder
-- scoped to the algorithm definition, not a specific run) -- Louvain output
-- can change between library versions of the SAME backend over time, so
-- this needs per-run granularity, not a one-time seed value. ADD COLUMN IF
-- NOT EXISTS makes this idempotent: re-running this file (db/init_db.py's
-- normal apply_schema()) against an already-provisioned database applies
-- the migration in place, no separate migration runner needed.
ALTER TABLE analysis_run ADD COLUMN IF NOT EXISTS backend_library_version VARCHAR(40);

COMMIT;
