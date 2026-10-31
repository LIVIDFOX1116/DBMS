"""The one SQLAlchemy engine the API uses for live data.

Built from exactly the connection logic api/main.py's psycopg2 pool already
uses, so the ORM inherits its fixes rather than relearning them:

- the DSN comes from ``dbsetup.current_dsn()``: the runtime settings the
  desktop app pushes over /internal/db-settings, else PG* from the
  environment / .env (ingestion/config.py). No credential is written here.
- every new connection goes through ``dbsetup.connect()``, which tries
  127.0.0.1 before the name ``localhost`` -- worth ~2 s per connection on
  Windows (see its docstring).

Lazy, like the pool: mock mode (the default, and everything CI runs) never
calls get_engine(), so it never needs Postgres. ``reset_engine()`` is called
from api.main.close_pool(), i.e. on shutdown and whenever the connection
settings or the mock/live mode change -- a pooled connection to the old
database must not outlive the setting that created it.
"""
import logging
import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

log = logging.getLogger("db.orm.engine")

_engine: Engine | None = None
_engine_lock = threading.Lock()

# Same ceiling as the psycopg2 pool's maxconn=10. The two pools coexist while
# some endpoints are still on raw SQL; 20 connections at most is well under
# PostgreSQL's default max_connections of 100.
POOL_SIZE = 5
MAX_OVERFLOW = 5


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:  # re-check: another thread may have built it while we waited
                # Imported here, not at module level: graph/ imports the
                # models from this package and must not pull in the API.
                from api import dbsetup

                dsn = dbsetup.current_dsn()
                _engine = create_engine(
                    "postgresql+psycopg2://",
                    creator=lambda: dbsetup.connect(dsn),
                    pool_size=POOL_SIZE,
                    max_overflow=MAX_OVERFLOW,
                    # A connection the server dropped (restart, idle timeout,
                    # the WSL relay going quiet) is detected and replaced on
                    # checkout instead of failing the request that drew it.
                    pool_pre_ping=True,
                )
                log.info("SQLAlchemy engine created (pool_size=%d, max_overflow=%d)", POOL_SIZE, MAX_OVERFLOW)
    return _engine


def reset_engine() -> None:
    """Disposes the engine so the next session builds a fresh one against
    whatever the settings now say."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
            log.info("SQLAlchemy engine disposed")
            _engine = None
