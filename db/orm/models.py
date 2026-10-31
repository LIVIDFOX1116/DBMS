"""Declarative models for all 26 FrameCheck tables.

These DESCRIBE tables that db/schema.sql and db/migrations/*.sql create;
they never create them. There is deliberately no ``Base.metadata.create_all()``
anywhere in the project -- the SQL files are the single source of truth for
DDL, applied in order by api/dbsetup.py and tracked in schema_migration.
tests/test_orm_models.py compiles every model against the PostgreSQL dialect
and fails if a column, type, nullability, key or default stops matching the
DDL, so a migration that forgets the model (or vice versa) is caught offline.

Defaults are ``server_default`` copies of the DDL's: when the ORM inserts a
row it leaves those columns out and PostgreSQL fills them, exactly as it
does for the raw-SQL inserts elsewhere. Indexes and CHECK constraints are
NOT repeated here -- the database enforces them, and a second copy could only
drift.

Two tables behave differently on purpose (migration 009):

- ``flag_audit`` is append-only and hash-chained. A BEFORE INSERT trigger
  computes ``prev_hash``/``row_hash``; a BEFORE UPDATE OR DELETE trigger
  refuses edits. The model marks both hash columns as server-generated so
  the ORM never sends them and reads back what the trigger computed, and
  the flush guard below refuses ORM updates and deletes before they reach
  the trigger.
- ``flag_history`` and ``flag_audit_tombstone`` are written only by triggers.
  They are mapped for reading; the flush guard refuses any ORM write.
"""
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    BigInteger,
    Boolean,
    FetchedValue,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, DOUBLE_PRECISION, JSONB, TSTZRANGE
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

TS = TIMESTAMP(timezone=False)        # TIMESTAMP (without time zone) -- most of the schema
TSTZ = TIMESTAMP(timezone=True)       # TIMESTAMPTZ -- migrations 006 and 009
WEI = Numeric(38, 0)                  # wei amounts: unsigned, far beyond BIGINT
NOW = text("now()")


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Ingestion (db/schema.sql)
# ---------------------------------------------------------------------------

class Wallet(Base):
    __tablename__ = "wallet"

    wallet_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    address: Mapped[str] = mapped_column(CHAR(42), nullable=False, unique=True)
    address_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'EOA'"))
    first_seen_block: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_block: Mapped[int | None] = mapped_column(BigInteger)
    balance_wei: Mapped[Decimal | None] = mapped_column(WEI, server_default=text("0"))
    total_sent_wei: Mapped[Decimal | None] = mapped_column(WEI, server_default=text("0"))
    total_recv_wei: Mapped[Decimal | None] = mapped_column(WEI, server_default=text("0"))
    tx_count: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class Block(Base):
    __tablename__ = "block"

    block_number: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    block_hash: Mapped[str] = mapped_column(CHAR(66), nullable=False, unique=True)
    proposer_wallet_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"))
    block_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    gas_used: Mapped[int | None] = mapped_column(BigInteger)
    gas_limit: Mapped[int | None] = mapped_column(BigInteger)
    tx_count: Mapped[int | None] = mapped_column(Integer)

    proposer: Mapped[Wallet | None] = relationship()


class Transaction(Base):
    __tablename__ = "transaction"

    tx_hash: Mapped[str] = mapped_column(CHAR(66), primary_key=True)
    block_number: Mapped[int] = mapped_column(BigInteger, ForeignKey("block.block_number"), nullable=False)
    # The SIGNER -- the only trustworthy "who sent this" (CLAUDE.md).
    from_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    to_wallet_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"))
    value_wei: Mapped[Decimal] = mapped_column(WEI, nullable=False)
    gas_price: Mapped[int | None] = mapped_column(BigInteger)
    gas_used: Mapped[int | None] = mapped_column(BigInteger)
    tx_index: Mapped[int | None] = mapped_column(SmallInteger)
    nonce: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[bool | None] = mapped_column(Boolean)
    method_id: Mapped[str | None] = mapped_column(CHAR(10))
    is_simulated: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))   # migration 002

    block: Mapped[Block] = relationship()
    signer: Mapped[Wallet] = relationship(foreign_keys=[from_wallet_id])
    recipient: Mapped[Wallet | None] = relationship(foreign_keys=[to_wallet_id])


class Token(Base):
    __tablename__ = "token"

    token_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False, unique=True)
    contract_address: Mapped[str] = mapped_column(CHAR(42), nullable=False, unique=True)
    symbol: Mapped[str | None] = mapped_column(String(128))
    token_name: Mapped[str | None] = mapped_column(String(255))
    decimals: Mapped[int | None] = mapped_column(SmallInteger)
    standard: Mapped[str | None] = mapped_column(String(10), server_default=text("'ERC20'"))

    # One-to-one subtype link: the contract IS a wallet row.
    contract_wallet: Mapped[Wallet] = relationship()


class TokenTransfer(Base):
    __tablename__ = "token_transfer"
    __table_args__ = (UniqueConstraint("tx_hash", "log_index"),)

    transfer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tx_hash: Mapped[str] = mapped_column(CHAR(66), ForeignKey("transaction.tx_hash"), nullable=False)
    token_id: Mapped[int] = mapped_column(Integer, ForeignKey("token.token_id"), nullable=False)
    # From the ERC-20 Transfer event log, which a spam contract can forge.
    # Never a signer: the signer is transaction.from_wallet_id via tx_hash.
    from_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    to_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    token_amount: Mapped[Decimal | None] = mapped_column(Numeric(78, 0))
    log_index: Mapped[int | None] = mapped_column(SmallInteger)

    transaction: Mapped[Transaction] = relationship()
    token: Mapped[Token] = relationship()
    from_wallet: Mapped[Wallet] = relationship(foreign_keys=[from_wallet_id])
    to_wallet: Mapped[Wallet] = relationship(foreign_keys=[to_wallet_id])


class Label(Base):
    __tablename__ = "label"

    label_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label_name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    category: Mapped[str | None] = mapped_column(String(30))
    source: Mapped[str | None] = mapped_column(String(40))
    is_sanctioned: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))


class Analyst(Base):
    __tablename__ = "analyst"

    analyst_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(CHAR(60), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'analyst'"))
    organisation: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class WalletLabel(Base):
    __tablename__ = "wallet_label"

    wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), primary_key=True)
    label_id: Mapped[int] = mapped_column(Integer, ForeignKey("label.label_id"), primary_key=True)
    tagged_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("analyst.analyst_id"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    tagged_at: Mapped[datetime | None] = mapped_column(TS, server_default=NOW)

    wallet: Mapped[Wallet] = relationship()
    label: Mapped[Label] = relationship()
    tagger: Mapped[Analyst | None] = relationship()


# ---------------------------------------------------------------------------
# Snapshot and analysis (db/schema.sql)
# ---------------------------------------------------------------------------

class GraphSnapshot(Base):
    __tablename__ = "graph_snapshot"

    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'parquet'"))
    start_block: Mapped[int | None] = mapped_column(BigInteger)
    end_block: Mapped[int | None] = mapped_column(BigInteger)
    vertex_count: Mapped[int | None] = mapped_column(BigInteger)
    edge_count: Mapped[int | None] = mapped_column(BigInteger)
    is_directed: Mapped[bool | None] = mapped_column(Boolean, server_default=text("true"))
    checksum_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime | None] = mapped_column(TS, server_default=NOW)

    runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="snapshot")
    edges: Mapped[list["SnapshotEdge"]] = relationship(back_populates="snapshot")


class SnapshotEdge(Base):
    __tablename__ = "snapshot_edge"
    __table_args__ = (UniqueConstraint("snapshot_id", "src_wallet_id", "dst_wallet_id"),)

    edge_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("graph_snapshot.snapshot_id"), nullable=False)
    src_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    dst_wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    edge_weight: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    transfer_count: Mapped[int | None] = mapped_column(Integer, server_default=text("1"))

    snapshot: Mapped[GraphSnapshot] = relationship(back_populates="edges")
    src_wallet: Mapped[Wallet] = relationship(foreign_keys=[src_wallet_id])
    dst_wallet: Mapped[Wallet] = relationship(foreign_keys=[dst_wallet_id])


class SnapshotEdgeTx(Base):
    __tablename__ = "snapshot_edge_tx"

    snapshot_edge_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("snapshot_edge.edge_id"), primary_key=True)
    tx_hash: Mapped[str] = mapped_column(CHAR(66), ForeignKey("transaction.tx_hash"), primary_key=True)

    edge: Mapped[SnapshotEdge] = relationship()
    transaction: Mapped[Transaction] = relationship()


class Algorithm(Base):
    __tablename__ = "algorithm"

    algorithm_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    algorithm_name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    cugraph_function: Mapped[str | None] = mapped_column(String(60))
    default_params: Mapped[Any | None] = mapped_column(JSONB)
    library_version: Mapped[str | None] = mapped_column(String(20))


class AnalysisRun(Base):
    __tablename__ = "analysis_run"

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("graph_snapshot.snapshot_id"), nullable=False)
    algorithm_id: Mapped[int] = mapped_column(Integer, ForeignKey("algorithm.algorithm_id"), nullable=False)
    analyst_id: Mapped[int] = mapped_column(Integer, ForeignKey("analyst.analyst_id"), nullable=False)
    parameters: Mapped[Any | None] = mapped_column(JSONB)
    gpu_device: Mapped[str | None] = mapped_column(String(40))       # the backend name, CPU or GPU
    gpu_memory_mb: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(12), server_default=text("'PENDING'"))
    backend_library_version: Mapped[str | None] = mapped_column(String(40))

    snapshot: Mapped[GraphSnapshot] = relationship(back_populates="runs")
    algorithm: Mapped[Algorithm] = relationship()
    analyst: Mapped[Analyst] = relationship()
    clusters: Mapped[list["Cluster"]] = relationship(back_populates="run")
    metrics: Mapped[list["VertexMetric"]] = relationship(back_populates="run")
    flags: Mapped[list["Flag"]] = relationship(back_populates="run")


class VertexMetric(Base):
    __tablename__ = "vertex_metric"
    __table_args__ = (UniqueConstraint("run_id", "wallet_id", "metric_name"),)

    metric_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("analysis_run.run_id"), nullable=False)
    wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(30), nullable=False)
    metric_value: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    rank_position: Mapped[int | None] = mapped_column(Integer)

    run: Mapped[AnalysisRun] = relationship(back_populates="metrics")
    wallet: Mapped[Wallet] = relationship()


class Cluster(Base):
    __tablename__ = "cluster"

    cluster_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("analysis_run.run_id"), nullable=False)
    community_label: Mapped[int] = mapped_column(Integer, nullable=False)
    member_count: Mapped[int | None] = mapped_column(Integer)
    internal_edges: Mapped[int | None] = mapped_column(BigInteger)
    modularity_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    cluster_type: Mapped[str | None] = mapped_column(String(20))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    run: Mapped[AnalysisRun] = relationship(back_populates="clusters")
    members: Mapped[list["ClusterMember"]] = relationship(back_populates="cluster")


class ClusterMember(Base):
    __tablename__ = "cluster_member"

    cluster_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cluster.cluster_id"), primary_key=True)
    wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), primary_key=True)
    membership_score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    is_core_node: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    in_degree: Mapped[int | None] = mapped_column(Integer)
    out_degree: Mapped[int | None] = mapped_column(Integer)

    cluster: Mapped[Cluster] = relationship(back_populates="members")
    wallet: Mapped[Wallet] = relationship()


class Alert(Base):
    __tablename__ = "alert"

    alert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("analysis_run.run_id"), nullable=False)
    cluster_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cluster.cluster_id"))
    wallet_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"))
    reviewed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("analyst.analyst_id"))
    pattern_type: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    detected_at: Mapped[datetime | None] = mapped_column(TS, server_default=NOW)
    review_status: Mapped[str | None] = mapped_column(String(12), server_default=text("'NEW'"))

    run: Mapped[AnalysisRun] = relationship()
    cluster: Mapped[Cluster | None] = relationship()
    wallet: Mapped[Wallet | None] = relationship()
    reviewer: Mapped[Analyst | None] = relationship()


class Visualization(Base):
    __tablename__ = "visualization"

    viz_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("analysis_run.run_id"), nullable=False)
    layout_algorithm: Mapped[str | None] = mapped_column(String(40))
    image_path: Mapped[str | None] = mapped_column(String(255))
    image_format: Mapped[str | None] = mapped_column(String(10))
    nodes_rendered: Mapped[int | None] = mapped_column(Integer)
    edges_rendered: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime | None] = mapped_column(TS, server_default=NOW)

    run: Mapped[AnalysisRun] = relationship()


# ---------------------------------------------------------------------------
# FrameCheck audit (migrations 002, 005, 009)
# ---------------------------------------------------------------------------

class FramingScenario(Base):
    __tablename__ = "framing_scenario"

    scenario_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Arrays of wallet ids: PostgreSQL cannot put a foreign key on these.
    attacker_wallet_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    target_wallet_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    transfer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_value_wei: Mapped[Decimal] = mapped_column(WEI, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class Flag(Base):
    __tablename__ = "flag"

    flag_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("analysis_run.run_id"), nullable=False)
    wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallet.wallet_id"), nullable=False)
    detector: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    threshold: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    token_contract: Mapped[str | None] = mapped_column(CHAR(42))     # migration 005

    run: Mapped[AnalysisRun] = relationship(back_populates="flags")
    wallet: Mapped[Wallet] = relationship()
    evidence: Mapped[list["FlagEvidence"]] = relationship(back_populates="flag")
    audits: Mapped[list["FlagAudit"]] = relationship(back_populates="flag")


class FlagEvidence(Base):
    __tablename__ = "flag_evidence"

    flag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flag.flag_id"), primary_key=True)
    tx_hash: Mapped[str] = mapped_column(CHAR(66), ForeignKey("transaction.tx_hash"), primary_key=True)
    hop: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)    # 'signed' | 'unsolicited'

    flag: Mapped[Flag] = relationship(back_populates="evidence")
    transaction: Mapped[Transaction] = relationship()


class FlagAudit(Base):
    __tablename__ = "flag_audit"
    # Fetch the trigger-computed hashes in the INSERT's RETURNING, so a
    # freshly added audit row carries its real row_hash without a re-query.
    __mapper_args__ = {"eager_defaults": True}

    flag_audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flag.flag_id"), nullable=False)
    verdict: Mapped[str] = mapped_column(String(12), nullable=False)  # 'frameable' | 'robust'
    framing_cost_wei: Mapped[Decimal | None] = mapped_column(WEI)
    framing_gas: Mapped[int | None] = mapped_column(BigInteger)
    minimal_tx_set: Mapped[Any | None] = mapped_column(JSONB)
    method: Mapped[str] = mapped_column(String(20), nullable=False)  # 'exact' | 'greedy_upper_bound' | 'yes_no'
    backend: Mapped[str | None] = mapped_column(String(40))
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    # Computed by the flag_audit_chain trigger (migration 009). FetchedValue:
    # never sent by the ORM, always read back from the database.
    prev_hash: Mapped[bytes | None] = mapped_column(BYTEA, server_default=FetchedValue())
    row_hash: Mapped[bytes | None] = mapped_column(BYTEA, server_default=FetchedValue())

    flag: Mapped[Flag] = relationship(back_populates="audits")


class FlagHistory(Base):
    """Read-only: written only by the flag_versioning trigger."""
    __tablename__ = "flag_history"

    flag_history_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flag_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # no FK by design: outlives the flag
    change_op: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    before_image: Mapped[Any] = mapped_column(JSONB, nullable=False)
    sys_period: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)


class FlagAuditTombstone(Base):
    """Read-only: written only by the flag_audit_append_only trigger."""
    __tablename__ = "flag_audit_tombstone"

    tombstone_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    flag_audit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)   # no FK: that row is gone
    flag_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(12))
    row_hash: Mapped[bytes | None] = mapped_column(BYTEA)
    deleted_by: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(TSTZ, nullable=False, server_default=NOW)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# App and ops (migration 006, api/dbsetup.py)
# ---------------------------------------------------------------------------

class AppUser(Base):
    """Google sign-in identity, keyed on `sub`, never on email. No FK in
    either direction by design (migration 006)."""
    __tablename__ = "app_user"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    google_sub: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    picture_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TSTZ, nullable=False, server_default=NOW)
    last_login_at: Mapped[datetime | None] = mapped_column(TSTZ)


class SchemaMigration(Base):
    """Which migration files are applied. Written by api/dbsetup.py, which
    owns DDL; mapped here so the model set covers every table."""
    __tablename__ = "schema_migration"

    filename: Mapped[str] = mapped_column(String(200), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

READ_ONLY_MODELS = (FlagHistory, FlagAuditTombstone)


class ReadOnlyTableError(RuntimeError):
    """An ORM write to a table that only the database's triggers may write."""


@event.listens_for(Session, "before_flush")
def _guard_trigger_owned_tables(session, flush_context, instances):
    """Refuses, before anything reaches PostgreSQL, the writes migration 009's
    triggers would refuse anyway -- with a message that says why instead of a
    restrict_violation from deep inside a flush."""
    for obj in session.new | session.dirty | session.deleted:
        if isinstance(obj, READ_ONLY_MODELS):
            raise ReadOnlyTableError(
                f"{type(obj).__tablename__} is written only by database triggers; "
                "the ORM maps it for reading")
    for obj in session.deleted:
        if isinstance(obj, FlagAudit):
            raise ReadOnlyTableError(
                "flag_audit is append-only: a corrected verdict is a new audit row, not a deletion")
    for obj in session.dirty:
        if isinstance(obj, FlagAudit) and session.is_modified(obj, include_collections=False):
            raise ReadOnlyTableError(
                "flag_audit is append-only: a corrected verdict is a new audit row, not an edit")
