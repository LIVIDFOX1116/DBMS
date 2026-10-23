#!/usr/bin/env python
"""
demo_schemas/demo_db.py -- standalone schema-management demo tool.

Everything here lives in its own Postgres schema namespace, `demo`, inside
the same database FrameCheck already uses (reads the same .env / PG*
environment variables, via api.dbsetup.connect/current_dsn -- reused, not
modified, including its IPv4-first connection fix) -- but it is otherwise
completely separate from FrameCheck's own tables, which all live in
`public`. Dropping `demo` cascade cannot touch a single wallet/transaction/
cluster row.

Not part of the app or its API: a terminal tool built new for one purpose,
a live schema-management demo. See REVIEW_GUIDE.md's "Live Demo: Schema
Management" section for the exact command sequence to run.

Commands:
    seed            create `demo` (if absent) and load the 3 base schemas
    reset           drop `demo` cascade, then seed again -- instant recovery
                    if something breaks mid-demo
    list            show every table in `demo` with its row count
    load <file>     run a .sql file against `demo` (DDL or DML) -- used for
                    the live schema upload, each modification, and each
                    invalid-schema example. Refuses a file that mentions
                    `public` or a FrameCheck table name unless
                    --allow-public is passed, so a typo cannot reach the
                    real database live on stage.
    query <file>    run a .sql file's SELECT statement(s), each as its own
                    result set -- used for the before/after proof queries.
"""
import argparse
import pathlib
import sys

import psycopg2

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.dbsetup import connect, current_dsn  # noqa: E402 (needs sys.path set first)

DEMO_DIR = pathlib.Path(__file__).resolve().parent
BASE_SCHEMA_FILES = sorted(DEMO_DIR.glob("0*.sql"))

# FrameCheck's own tables (db/schema.sql) -- a `load`/`query` file mentioning
# one of these, or writing to `public`, is almost certainly a demo-day typo
# and is refused rather than silently run against the real database.
FRAMECHECK_TABLE_HINTS = (
    "public.", "wallet", "transaction", "cluster", "flag_audit",
    "token_transfer", "snapshot_edge", "vertex_metric", "analysis_run",
)


def _guard_public(sql_text: str, allow_public: bool) -> None:
    if allow_public:
        return
    lowered = sql_text.lower()
    hit = next((h for h in FRAMECHECK_TABLE_HINTS if h in lowered), None)
    if hit:
        raise SystemExit(
            f"refusing to run this file: it mentions {hit!r}, which looks like FrameCheck's "
            "own schema (public), not the demo namespace. Pass --allow-public if this is "
            "intentional."
        )


def _connect():
    return connect(current_dsn())


def cmd_seed(args):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS demo")
        for path in BASE_SCHEMA_FILES:
            print(f"-- applying {path.name}")
            with conn:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
        print(f"seeded {len(BASE_SCHEMA_FILES)} base schema file(s) into `demo`.")
    finally:
        conn.close()


def cmd_reset(args):
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS demo CASCADE")
        print("dropped `demo` schema (cascade).")
    finally:
        conn.close()
    cmd_seed(args)


def cmd_list(args):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'demo' AND c.relkind = 'r'
                ORDER BY c.relname
            """)
            names = [row[0] for row in cur.fetchall()]
        if not names:
            print("`demo` schema is empty or does not exist yet -- run `seed` first.")
            return
        print(f"{'table':<20} rows")
        for name in names:
            with conn.cursor() as cur2:
                cur2.execute(f'SELECT count(*) FROM demo."{name}"')
                exact = cur2.fetchone()[0]
            print(f"{name:<20} {exact}")
    finally:
        conn.close()


def cmd_load(args):
    path = pathlib.Path(args.file)
    sql_text = path.read_text(encoding="utf-8")
    _guard_public(sql_text, args.allow_public)
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql_text)
        print(f"OK: {path.name} applied successfully.")
    except psycopg2.Error as e:
        print(f"REJECTED: {path.name}")
        print(f"  {str(e).strip()}")
        sys.exit(1)
    finally:
        conn.close()


def _split_statements(sql_text: str) -> list:
    """Splits on top-level semicolons so each SELECT in a multi-query file
    gets its own result set. Strips comment-only LINES out of each chunk
    first (not just a whole-chunk prefix check) -- a chunk preceded by a
    multi-line header comment still starts with "--" as a whole string even
    though real SQL follows a few lines down, which silently dropped the
    first statement in a file that opens with a comment block. Good enough
    for this demo's own files (no semicolons inside string literals); not a
    general SQL parser."""
    parts = []
    for raw in sql_text.split(";"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            parts.append(stmt)
    return parts


def cmd_query(args):
    path = pathlib.Path(args.file)
    sql_text = path.read_text(encoding="utf-8")
    _guard_public(sql_text, args.allow_public)
    conn = _connect()
    try:
        for i, stmt in enumerate(_split_statements(sql_text), start=1):
            with conn.cursor() as cur:
                cur.execute(stmt)
                if cur.description is None:
                    print(f"[{i}] (no result set -- statement executed)")
                    continue
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
            print(f"[{i}] " + " | ".join(columns))
            print("-" * (sum(len(c) for c in columns) + 3 * len(columns)))
            for row in rows:
                print(" | ".join(str(v) for v in row))
            print(f"({len(rows)} row(s))\n")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="demo_schemas schema-management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="create `demo` and load the 3 base schemas")
    sub.add_parser("reset", help="drop `demo` cascade, then seed again")
    sub.add_parser("list", help="list tables in `demo` with row counts")

    p_load = sub.add_parser("load", help="run a .sql file against `demo` (DDL/DML)")
    p_load.add_argument("file")
    p_load.add_argument("--allow-public", action="store_true",
                         help="allow a file that mentions `public` or a FrameCheck table name")

    p_query = sub.add_parser("query", help="run a .sql file's SELECT(s) and print each result")
    p_query.add_argument("file")
    p_query.add_argument("--allow-public", action="store_true")

    args = parser.parse_args()
    {"seed": cmd_seed, "reset": cmd_reset, "list": cmd_list,
     "load": cmd_load, "query": cmd_query}[args.command](args)


if __name__ == "__main__":
    main()
