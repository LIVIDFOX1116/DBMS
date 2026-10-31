"""``get_session()``: the ORM counterpart of api.main.get_conn().

    with get_session() as session:
        rows = session.execute(select(Wallet.wallet_id, Wallet.address)).all()

Commits when the block exits normally, rolls back when it raises, and always
closes. A connection-level failure (``OperationalError``/``InterfaceError``
-- e.g. Postgres restarting mid-request) invalidates the connection so it is
discarded rather than handed to the next request, the same guarantee
get_conn() gives its psycopg2 pool. Callers then see the error; api.main
turns it into a 503.

A context manager rather than a FastAPI ``Depends``: endpoints check mock
mode first and only open a session in live mode, and tests replace
``api.main.get_session`` the same way they replace ``get_conn``.
"""
from contextlib import contextmanager

from sqlalchemy.dialects.postgresql import psycopg2 as pg_psycopg2
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from db.orm.engine import get_engine

_PSYCOPG2 = pg_psycopg2.dialect()


def execute_on_cursor(cur, statement) -> list:
    """Runs an ORM-built statement on an open psycopg2 cursor and returns its
    rows (empty for a statement with no result). Values stay bound
    parameters; expanding IN-lists are rendered for the driver."""
    compiled = statement.compile(dialect=_PSYCOPG2, compile_kwargs={"render_postcompile": True})
    cur.execute(str(compiled), compiled.params)
    return cur.fetchall() if cur.description else []


def execute_on_connection(conn, statement) -> list:
    """Runs an ORM-built statement on a caller's existing psycopg2 connection,
    inside whatever transaction that connection is already in.

    For code that is handed a connection by its caller rather than opening
    its own: scripts/benchmark_audit.py and scripts/run_experiment.py write
    with commit=False and then roll back, and an API job runs on the
    connection its worker thread opened, so a helper that opened a separate
    Session would commit behind them or hold a second connection. The
    statement is still built from the models -- same columns, same
    server-generated hashes left out -- just executed where the caller's
    transaction lives. An INSERT compiles with RETURNING <primary key>; its
    row comes back like any other."""
    with conn.cursor() as cur:
        return execute_on_cursor(cur, statement)


@contextmanager
def get_session():
    # expire_on_commit=False: endpoints build their response from loaded
    # objects after the block has committed; expiring them would trigger a
    # lazy reload against a session that is already closed.
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except (OperationalError, InterfaceError):
        session.invalidate()
        raise
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
