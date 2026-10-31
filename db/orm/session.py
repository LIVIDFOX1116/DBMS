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

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from db.orm.engine import get_engine


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
