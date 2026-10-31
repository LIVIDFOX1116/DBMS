"""
Create the database (if needed), apply schema.sql if the schema is absent,
then every migration that has not been applied yet.

Usage:
    python db/init_db.py            # create + apply what is pending
    python db/init_db.py --adopt    # a database built before migration tracking

Reads connection settings from environment variables (see .env.example):
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

This only needs Postgres itself -- no GPU, no RAPIDS.

Delegates to api/dbsetup.py, the same code the app's Database view runs, so
both record what they applied in `schema_migration`. This script used to
re-run schema.sql and EVERY migration on every run, relying on each being
idempotent. That stopped being safe at migration 010, which drops `analyst`:
re-running schema.sql would recreate it (CREATE TABLE IF NOT EXISTS) and
re-seed its `system` row. Tracked application runs schema.sql only when the
schema is absent, and each migration once.
"""
# RAW SQL (allowed): DDL -- creates the database and applies db/schema.sql + migrations (via api/dbsetup.py)
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from api import dbsetup  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--adopt", action="store_true",
                        help="record the migrations whose work is already present (a database from before tracking), "
                             "then apply the rest")
    args = parser.parse_args(argv)

    dsn = dbsetup.current_dsn()
    created = dbsetup.create_database(dsn)
    print(created["detail"])

    conn = dbsetup.connect(dsn)
    try:
        state = dbsetup.schema_state(conn)
        if state["state"] == "untracked":
            if not args.adopt:
                print(f"'{dsn['dbname']}' has FrameCheck tables but no migration tracking, so which migrations "
                      "already ran is unknown. Re-run with --adopt to record the ones whose work is present "
                      "(each migration's probe decides) and apply the rest.", file=sys.stderr)
                return 2
            adopted = dbsetup.mark_existing_as_applied(conn)
            print(f"adopted: {adopted}")
        result = dbsetup.apply_schema(conn)
        if result["applied"]:
            for name in result["applied"]:
                print(f"applied {name}")
        else:
            print(f"'{dsn['dbname']}' is up to date ({len(result['state']['migrations_applied'])} migrations recorded)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except psycopg2.OperationalError as e:
        print(f"Could not connect to Postgres: {e}", file=sys.stderr)
        print("Is Postgres running? Check PGHOST/PGPORT/PGUSER/PGPASSWORD in your .env", file=sys.stderr)
        sys.exit(1)
