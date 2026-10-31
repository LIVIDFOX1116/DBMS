"""Optional: the ORM against a real PostgreSQL, proving the parts no offline
test can -- that migration 009's triggers and the ORM agree.

Skipped unless FRAMECHECK_TEST_PG_DSN is set, so `pytest tests/` stays fully
offline. Point it at a THROWAWAY database, never the real one:

    FRAMECHECK_TEST_PG_DSN="host=127.0.0.1 port=5432 dbname=framecheck_orm_test user=postgres password=..." \\
        python -m pytest tests/test_orm_integration.py -v

It refuses a database named `framecheck`. If the target is empty it applies
db/schema.sql + migrations (api/dbsetup.apply_schema); every write happens
inside a transaction that is always rolled back, so it leaves no rows and
never needs DROP.
"""
import os

import pytest

DSN = os.getenv("FRAMECHECK_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="FRAMECHECK_TEST_PG_DSN not set (live PostgreSQL test)")


@pytest.fixture(scope="module")
def engine():
    import psycopg2
    from psycopg2.extensions import parse_dsn
    from sqlalchemy import create_engine

    from api import dbsetup

    if parse_dsn(DSN).get("dbname") == "framecheck":
        pytest.fail("refusing to run against the `framecheck` database -- use a throwaway one")
    setup = psycopg2.connect(DSN)
    try:
        if dbsetup.schema_state(setup)["state"] == "empty":
            dbsetup.apply_schema(setup)
    finally:
        setup.close()
    eng = create_engine("postgresql+psycopg2://", creator=lambda: psycopg2.connect(DSN))
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """A session whose commits are savepoints inside one outer transaction
    that is always rolled back."""
    from sqlalchemy.orm import Session

    with engine.connect() as conn:
        outer = conn.begin()
        s = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            s.close()
            outer.rollback()


@pytest.fixture
def flag(session):
    from sqlalchemy import select

    from db.orm.models import Algorithm, AnalysisRun, Analyst, Flag, GraphSnapshot, Wallet

    wallet = Wallet(address="0x" + "ab" * 20)
    snapshot = GraphSnapshot(snapshot_name="orm-integration-test", file_path="snapshots/none.parquet")
    session.add_all([wallet, snapshot])
    session.flush()
    run = AnalysisRun(snapshot_id=snapshot.snapshot_id,
                      algorithm_id=session.scalar(select(Algorithm.algorithm_id).where(Algorithm.algorithm_name == "bfs_proximity")),
                      analyst_id=session.scalar(select(Analyst.analyst_id).where(Analyst.username == "system")),
                      status="DONE")
    session.add(run)
    session.flush()
    f = Flag(run_id=run.run_id, wallet_id=wallet.wallet_id, detector="bfs_proximity", score=1.0, threshold=0.5)
    session.add(f)
    session.flush()
    return f


def test_orm_insert_into_flag_audit_gets_its_hash_from_the_trigger(session, flag):
    from db.orm.models import FlagAudit

    audit = FlagAudit(flag_id=flag.flag_id, verdict="robust", method="yes_no", backend="orm-integration")
    session.add(audit)
    session.flush()
    # eager_defaults: the trigger-computed values come back in RETURNING.
    assert audit.row_hash is not None and len(audit.row_hash) == 32
    assert audit.prev_hash is not None


def test_orm_update_of_flag_audit_is_refused_by_the_flush_guard(session, flag):
    from db.orm.models import FlagAudit, ReadOnlyTableError

    audit = FlagAudit(flag_id=flag.flag_id, verdict="robust", method="yes_no")
    session.add(audit)
    session.flush()
    audit.verdict = "frameable"
    with pytest.raises(ReadOnlyTableError):
        session.flush()


def test_a_bulk_orm_update_is_refused_by_the_database_trigger(session, flag):
    # update() statements bypass the flush guard; migration 009's trigger
    # is what actually stops them.
    from sqlalchemy import exc, update

    from db.orm.models import FlagAudit

    audit = FlagAudit(flag_id=flag.flag_id, verdict="robust", method="yes_no")
    session.add(audit)
    session.flush()
    with pytest.raises(exc.DBAPIError) as raised:
        session.execute(update(FlagAudit).where(FlagAudit.flag_audit_id == audit.flag_audit_id).values(verdict="frameable"))
    assert raised.value.orig.pgcode == "23001"     # restrict_violation, raised by flag_audit_guard()


def test_flag_changes_are_versioned_into_history(session, flag):
    from sqlalchemy import select

    from db.orm.models import FlagHistory

    flag.score = 2.0
    session.flush()
    history = session.scalars(select(FlagHistory).where(FlagHistory.flag_id == flag.flag_id)).all()
    assert [h.change_op for h in history] == ["U"]
    assert history[0].before_image["score"] == 1.0
