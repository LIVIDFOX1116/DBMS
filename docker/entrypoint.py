"""Container entrypoint: bring the database up to date, then serve.

1. Waits for PostgreSQL (compose's healthcheck covers startup order there, but
   a managed cloud database may be waking from idle).
2. Applies schema.sql if absent and any pending migrations, through the same
   tracked path as the app's Database view (api/dbsetup.apply_schema). Safe on
   every start. FRAMECHECK_SKIP_MIGRATIONS=1 turns it off.
3. With FRAMECHECK_IMPORT_DEMO=1, imports the bundled dataset, but only into a
   database with no analysis data yet: import never touches existing rows.
4. Replaces itself with uvicorn on $PORT (default 8000).
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from api import dbsetup  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("entrypoint")


def wait_for_database(dsn: dict, attempts: int = 30, delay: float = 2.0):
    last = None
    for _ in range(attempts):
        try:
            return dbsetup.connect(dsn)
        except psycopg2.OperationalError as e:
            last = e
            time.sleep(delay)
    raise SystemExit(f"database not reachable after {attempts} attempts: {last}")


def prepare_database() -> None:
    dsn = dbsetup.current_dsn()
    conn = wait_for_database(dsn)
    try:
        if os.getenv("FRAMECHECK_SKIP_MIGRATIONS", "0") != "1":
            applied = dbsetup.apply_schema(conn)["applied"]
            log.info("schema: %s", f"applied {applied}" if applied else "up to date")
        if os.getenv("FRAMECHECK_IMPORT_DEMO", "0") == "1":
            if dbsetup.dataset_target_rows(conn, ["wallet"]):
                log.info("demo dataset: skipped, this database already has data")
            else:
                result = dbsetup.import_dataset(conn, dbsetup.demo_dataset())
                log.info("demo dataset: imported %s rows", f"{result['total_rows']:,}")
    finally:
        conn.close()


def main() -> None:
    if os.getenv("FRAMECHECK_API_MODE", "mock").lower() == "live":
        prepare_database()
    port = os.getenv("PORT", "8000")
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    main()
