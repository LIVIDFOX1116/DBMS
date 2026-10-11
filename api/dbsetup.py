"""
Feature 1: everything needed to go from "freshly installed app" to "a local
FrameCheck database with data in it", driven from inside the app instead of
from a terminal.

An installed FrameCheck has no `.env`, no repo checkout and no psql, so until
now a user who installed Postgres still could not point the app at it --
`ingestion/config.PG_DSN` is read from the environment at import time. This
module adds a runtime connection setting on top of that (env stays the
default), plus the operations the Database view offers: create the database,
apply the schema and migrations, inspect what is actually in there, and
browse a table read-only.

Deliberately NOT here, per the repo's standing rules: anything that drops,
truncates or unscoped-deletes. Importing into a database that already holds
FrameCheck tables is refused rather than resolved by wiping it -- that
decision belongs to whoever owns the data, not to a button.

Secrets: the password never comes from the frontend. Rust holds it in the OS
keychain and pushes the whole DSN to the loopback-only, internal-token-gated
/internal/db-settings route at launch, exactly as it already does for the
Etherscan API key (src-tauri/src/commands.rs). Everything this module
returns to the UI has the password stripped.
"""
import logging
import pathlib
import re

import psycopg2
from psycopg2 import sql

from ingestion import config

log = logging.getLogger("api.dbsetup")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

# A table that only exists once schema.sql has been applied, and an object
# that only exists once the newest migration has -- used to tell "empty
# database", "schema but unknown migration state" and "current" apart
# without guessing.
SCHEMA_SENTINEL_TABLE = "wallet"
MIGRATION_TRACKING_TABLE = "schema_migration"

# The one DSN field a user must not be able to steer from the UI into
# somewhere unexpected; everything else is a plain value psycopg2 escapes.
DSN_FIELDS = ("host", "port", "dbname", "user", "password")

_runtime_dsn: dict | None = None


# --------------------------------------------------------------- pure helpers


def redact(dsn: dict) -> dict:
    """What the UI is allowed to see. Returns whether a password is set, not
    the password -- a "" and a real secret must be distinguishable in the UI
    (so it can say "password saved") without ever shipping the value."""
    return {
        "host": dsn.get("host", ""),
        "port": str(dsn.get("port", "")),
        "dbname": dsn.get("dbname", ""),
        "user": dsn.get("user", ""),
        "password_set": bool(dsn.get("password")),
    }


def normalise_dsn(raw: dict) -> dict:
    """Keeps only the five fields psycopg2 is being handed, trims them, and
    rejects an empty host/dbname/user outright -- a half-filled form should
    fail here with a clear message rather than inside libpq."""
    dsn = {}
    for field in DSN_FIELDS:
        value = raw.get(field)
        dsn[field] = "" if value is None else str(value).strip()
    missing = [f for f in ("host", "dbname", "user") if not dsn[f]]
    if missing:
        raise ValueError(f"missing required connection field(s): {', '.join(missing)}")
    if not dsn["port"]:
        dsn["port"] = "5432"
    if not dsn["port"].isdigit() or not (1 <= int(dsn["port"]) <= 65535):
        raise ValueError(f"port must be a number between 1 and 65535, got {dsn['port']!r}")
    return dsn


def migration_files() -> list:
    """Migration files in applied order. Sorted by the numeric prefix, not
    lexically, so a hypothetical 010 lands after 009 rather than after 001."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    files = [p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file()]

    def key(path):
        match = re.match(r"^(\d+)", path.name)
        return (int(match.group(1)) if match else 10**6, path.name)

    return sorted(files, key=key)


def pending_migrations(available: list, applied: set) -> list:
    """Which of `available` (filenames, in order) have not been recorded in
    `applied`. Comparison is by filename: a migration renamed after the fact
    would be re-applied, which is why they never are."""
    return [name for name in available if name not in applied]


def is_safe_identifier(name: str) -> bool:
    """Cheap pre-filter for a table name arriving from the UI. Not the actual
    defence -- that is checking the name against the live catalogue and
    quoting it with psycopg2.sql.Identifier -- but it rejects the obvious
    nonsense before a query is built at all."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name or ""))


# ------------------------------------------------------------ runtime setting


def current_dsn() -> dict:
    """The runtime DSN if one has been pushed in, otherwise the environment's
    (ingestion/config.PG_DSN), so a developer running from a checkout with a
    .env sees exactly the behaviour they always did."""
    if _runtime_dsn is not None:
        return dict(_runtime_dsn)
    return dict(config.PG_DSN)


def has_runtime_dsn() -> bool:
    """Whether the app is using settings a user saved, rather than the
    environment's -- the UI says which, so a developer with a .env is never
    confused about where the connection came from."""
    return _runtime_dsn is not None


def set_runtime_dsn(raw: dict) -> dict:
    """Installs a new connection setting. Returns the redacted result. The
    caller is responsible for resetting the connection pool -- this module
    does not reach into api.main's pool."""
    global _runtime_dsn
    _runtime_dsn = normalise_dsn(raw)
    log.info("runtime Postgres DSN set to %s:%s/%s as %s",
             _runtime_dsn["host"], _runtime_dsn["port"], _runtime_dsn["dbname"], _runtime_dsn["user"])
    return redact(_runtime_dsn)


def clear_runtime_dsn() -> None:
    global _runtime_dsn
    _runtime_dsn = None


# ------------------------------------------------------------------ operations


def connect(dsn: dict, dbname: str | None = None, autocommit: bool = False):
    """A one-off connection, outside api.main's pool: these operations run
    rarely, must not consume a pooled slot, and CREATE DATABASE needs its own
    autocommit connection anyway."""
    params = dict(dsn)
    if dbname is not None:
        params["dbname"] = dbname
    conn = psycopg2.connect(**params)
    if autocommit:
        conn.autocommit = True
    return conn


def server_probe(dsn: dict) -> dict:
    """Can we reach this server at all, and does the named database exist?
    Connects to the maintenance database ("postgres"), so it answers even
    when the target database has not been created yet."""
    try:
        conn = connect(dsn, dbname="postgres")
    except psycopg2.OperationalError as e:
        return {"reachable": False, "error": str(e).strip(), "database_exists": None, "server_version": None}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dsn["dbname"],))
            exists = cur.fetchone() is not None
        return {"reachable": True, "error": None, "database_exists": exists, "server_version": version}
    finally:
        conn.close()


def create_database(dsn: dict) -> dict:
    """Creates the configured database if it is missing. Never drops or
    recreates an existing one -- an existing database is reported as such and
    left untouched."""
    probe = server_probe(dsn)
    if not probe["reachable"]:
        raise RuntimeError(f"cannot reach the Postgres server: {probe['error']}")
    if probe["database_exists"]:
        return {"created": False, "detail": f"database {dsn['dbname']!r} already exists -- left untouched"}
    conn = connect(dsn, dbname="postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dsn["dbname"])))
    finally:
        conn.close()
    log.info("created database %s", dsn["dbname"])
    return {"created": True, "detail": f"database {dsn['dbname']!r} created"}


def applied_migrations(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (MIGRATION_TRACKING_TABLE,))
        if not cur.fetchone()[0]:
            return set()
        cur.execute(sql.SQL("SELECT filename FROM {}").format(sql.Identifier(MIGRATION_TRACKING_TABLE)))
        return {row[0] for row in cur.fetchall()}


def schema_state(conn) -> dict:
    """Three distinguishable states, because conflating them is how a setup
    step silently does the wrong thing:

    - `empty`: no FrameCheck tables. Everything can be applied in order.
    - `untracked`: FrameCheck tables exist but there is no migration
      tracking table, so which migrations ran is genuinely unknown. Reported
      as unknown rather than assumed current -- the developer databases that
      predate this feature are in exactly this state.
    - `tracked`: the tracking table exists and lists what has been applied.
    """
    available = [p.name for p in migration_files()]
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (SCHEMA_SENTINEL_TABLE,))
        has_schema = cur.fetchone()[0]
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (MIGRATION_TRACKING_TABLE,))
        has_tracking = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'""")
        table_count = cur.fetchone()[0]

    if not has_schema:
        state = "empty"
        applied = set()
    elif not has_tracking:
        state = "untracked"
        applied = set()
    else:
        state = "tracked"
        applied = applied_migrations(conn)

    return {
        "state": state,
        "schema_applied": bool(has_schema),
        "table_count": table_count,
        "migrations_available": available,
        "migrations_applied": sorted(applied),
        "migrations_pending": pending_migrations(available, applied) if state != "untracked" else None,
    }


def _record_migration(conn, filename: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL(
            "INSERT INTO {} (filename) VALUES (%s) ON CONFLICT (filename) DO NOTHING"
        ).format(sql.Identifier(MIGRATION_TRACKING_TABLE)), (filename,))


def _ensure_tracking_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {} (
                filename    varchar(200) PRIMARY KEY,
                applied_at  timestamp NOT NULL DEFAULT now()
            )""").format(sql.Identifier(MIGRATION_TRACKING_TABLE)))


def apply_schema(conn, include_migrations: bool = True) -> dict:
    """Applies schema.sql (only when the schema is absent) and then every
    unapplied migration, each in its own transaction so a failure half way
    leaves the earlier ones applied and recorded rather than rolling back to
    nothing.

    Refuses to touch a database in the `untracked` state: FrameCheck tables
    exist but which migrations ran is unknown, and re-running one that
    already ran (an ALTER TABLE ... ADD COLUMN, say) would fail confusingly.
    Those databases are the developer ones; `mark_existing_as_applied`
    exists for them.
    """
    state = schema_state(conn)
    if state["state"] == "untracked":
        raise RuntimeError(
            "this database already has FrameCheck tables but no migration tracking, so which "
            "migrations have run is unknown -- adopt it first (see mark_existing_as_applied)")

    applied_now = []
    if not state["schema_applied"]:
        sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql_text)
        applied_now.append(SCHEMA_PATH.name)
        log.info("applied %s", SCHEMA_PATH.name)

    with conn:
        _ensure_tracking_table(conn)

    if include_migrations:
        already = applied_migrations(conn)
        for path in migration_files():
            if path.name in already:
                continue
            with conn:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                _record_migration(conn, path.name)
            applied_now.append(path.name)
            log.info("applied migration %s", path.name)

    return {"applied": applied_now, "state": schema_state(conn)}


def mark_existing_as_applied(conn) -> dict:
    """Adopts a pre-existing database: records every migration file as
    applied WITHOUT running it. For the databases that predate migration
    tracking, whose objects are already there.

    This is a claim about history that cannot be verified from inside the
    database, so it is a separate, explicit action rather than something
    apply_schema() infers -- and it refuses to run on a database that has no
    FrameCheck tables at all, where the claim would be plainly false.
    """
    state = schema_state(conn)
    if not state["schema_applied"]:
        raise RuntimeError("this database has no FrameCheck tables, so there is nothing to adopt")
    with conn:
        _ensure_tracking_table(conn)
    with conn:
        for path in migration_files():
            _record_migration(conn, path.name)
    return {"adopted": [p.name for p in migration_files()], "state": schema_state(conn)}


def table_inventory(conn) -> list:
    """Every table in `public` with an ESTIMATED row count and its on-disk
    size, in one query.

    Estimates, not `count(*)`: counting every row of all 23 tables on a real
    FrameCheck database took **6.85 seconds** when measured (2.1M
    snapshot_edge_tx rows, 1.3M transactions), which is far too slow for a
    panel that renders on tab open. `reltuples` is maintained by
    ANALYZE/autovacuum and is accurate enough to tell a user whether a table
    has data in it.

    It reads -1 on a table that has never been analysed, which is reported as
    `null` + `estimated_unavailable` rather than shown as "-1 rows"; the UI
    offers an exact count per table (see exact_row_count) for when the number
    has to be right.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.relname,
                   c.reltuples,
                   pg_total_relation_size(c.oid)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             ORDER BY c.relname""")
        rows = cur.fetchall()
    out = []
    for name, reltuples, size in rows:
        estimated = int(reltuples) if reltuples is not None and reltuples >= 0 else None
        out.append({
            "table": name,
            "rows": estimated,
            "rows_are_estimates": True,
            "estimate_unavailable": estimated is None,
            "bytes": int(size),
        })
    return out


def exact_row_count(conn, table: str) -> dict:
    """`count(*)` for one table, for when the estimate is not good enough.
    Same catalogue check and identifier quoting as sample_table -- the name
    arrives from the UI."""
    if not is_safe_identifier(table):
        raise ValueError(f"not a valid table name: {table!r}")
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name = %s""",
                    (table,))
        if cur.fetchone() is None:
            raise LookupError(f"no such table in this database: {table}")
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        count = cur.fetchone()[0]
    return {"table": table, "rows": int(count), "rows_are_estimates": False}


def sample_table(conn, table: str, limit: int = 50) -> dict:
    """Read-only peek at one table. The name is checked against the live
    catalogue and then quoted as an identifier -- never interpolated -- so a
    value from the UI cannot become SQL."""
    if not is_safe_identifier(table):
        raise ValueError(f"not a valid table name: {table!r}")
    limit = max(1, min(int(limit), 500))
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name = %s""",
                    (table,))
        if cur.fetchone() is None:
            raise LookupError(f"no such table in this database: {table}")
        cur.execute(sql.SQL("SELECT * FROM {} LIMIT %s").format(sql.Identifier(table)), (limit,))
        columns = [d[0] for d in cur.description]
        rows = [[_jsonable(v) for v in row] for row in cur.fetchall()]
    return {"table": table, "columns": columns, "rows": rows, "limit": limit}


def _jsonable(value):
    """Postgres types the JSON encoder would choke on: Decimal (NUMERIC(78,0)
    wei values, which also exceed float precision, so they go out as
    strings), bytes, dates."""
    import datetime
    import decimal
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return value
