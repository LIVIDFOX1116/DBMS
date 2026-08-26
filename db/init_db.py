"""
Create the database (if needed) and apply schema.sql.

Usage:
    python db/init_db.py

Reads connection settings from environment variables (see .env.example):
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

This only needs Postgres itself -- no GPU, no RAPIDS. Safe to run on the
Zenbook. Runs on both the "control" connection (to CREATE DATABASE, which
can't happen inside a transaction) and the target database (to run schema.sql).
"""
import os
import sys
import pathlib

import psycopg2
from psycopg2 import sql as pgsql
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("PGHOST", "localhost")
PORT = os.getenv("PGPORT", "5432")
DBNAME = os.getenv("PGDATABASE", "txgraph")
USER = os.getenv("PGUSER", "postgres")
PASSWORD = os.getenv("PGPASSWORD", "")

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"


def ensure_database_exists():
    """Connect to the default 'postgres' db and CREATE DATABASE if missing."""
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname="postgres", user=USER, password=PASSWORD
    )
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DBNAME,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(DBNAME)))
            print(f"Created database '{DBNAME}'")
        else:
            print(f"Database '{DBNAME}' already exists")
    conn.close()


def apply_schema():
    conn = psycopg2.connect(host=HOST, port=PORT, dbname=DBNAME, user=USER, password=PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())
        conn.commit()
        print(f"Applied {SCHEMA_PATH.name} to '{DBNAME}'")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        ensure_database_exists()
        apply_schema()
    except psycopg2.OperationalError as e:
        print(f"Could not connect to Postgres: {e}", file=sys.stderr)
        print("Is Postgres running? Check PGHOST/PGPORT/PGUSER/PGPASSWORD in your .env", file=sys.stderr)
        sys.exit(1)
