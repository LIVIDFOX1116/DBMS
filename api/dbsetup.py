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
import gzip
import io
import json
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
# The dataset the app ships with, so a new install has something real to look
# at before anyone has a database, an API key, or an hour to wait for a crawl.
# Bundled into the sidecar (scripts/build_backend_sidecar.py), which is why
# this resolves relative to this file rather than the working directory.
DATASETS_DIR = REPO_ROOT / "datasets"
DEMO_DATASET = DATASETS_DIR / "snapshot1_polynetwork.sql.gz"

# A table that only exists once schema.sql has been applied, and an object
# that only exists once the newest migration has -- used to tell "empty
# database", "schema but unknown migration state" and "current" apart
# without guessing.
SCHEMA_SENTINEL_TABLE = "wallet"
MIGRATION_TRACKING_TABLE = "schema_migration"

# A dataset file written by scripts/export_dataset.py: psql-style COPY blocks
# in foreign-key order, with a manifest on the second line.
DATASET_MARKER = "-- framecheck-dataset v1"
# The tables a dataset is allowed to write to. A file is data, not
# instructions: its COPY headers are checked against this list rather than
# trusted, so an edited dataset cannot be used to write somewhere it should
# not -- app_user (real people's Google identities) above all.
DATASET_TABLES = frozenset({
    "analyst", "algorithm", "label", "framing_scenario", "wallet", "wallet_label",
    "block", "transaction", "token", "token_transfer", "graph_snapshot",
    "snapshot_edge", "snapshot_edge_tx", "analysis_run", "vertex_metric",
    "cluster", "cluster_member", "alert", "visualization",
    "flag", "flag_evidence", "flag_audit",
})

# Tables `db/schema.sql` seeds when it is applied, so a database that has only
# had its schema imported is NOT empty -- it already holds 1 analyst and 8
# algorithm rows. Found by trying an import on a freshly-created database and
# having it refused.
SEED_TABLES = frozenset({"analyst", "algorithm", "label"})

# The business key of each seed table: the column that means the same thing in
# every FrameCheck database, as opposed to the id, which does not.
#
# This distinction is the whole reason the remapping below exists. In the
# database this project has been developing against, `pagerank_threshold` is
# algorithm_id 12; in a database freshly created from schema.sql it is 6 --
# same name, different id, because ids were handed out as detectors were added
# over time. An import that trusted the dataset's ids died on
#   analysis_run_algorithm_id_fkey: Key (algorithm_id)=(10) is not present
# and merging the dataset's rows instead would have collided with the unique
# constraint on algorithm_name. So the ids are translated through the names.
SEED_KEY = {
    "algorithm": ("algorithm_id", "algorithm_name"),
    "analyst": ("analyst_id", "username"),
    "label": ("label_id", "label_name"),
}

# Columns that reference a seed table's id and therefore need translating.
SEED_REFERENCES = {
    "analysis_run": {"algorithm_id": "algorithm", "analyst_id": "analyst"},
    "wallet_label": {"tagged_by": "analyst", "label_id": "label"},
    "alert": {"reviewed_by": "analyst"},
}

COPY_NULL = "\\N"

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


PROBE_PATTERN = re.compile(r"^--\s*probe:\s*(.+)$", re.MULTILINE)


def migration_probe(path) -> str:
    """The SQL a migration offers for "is your work already in this database?"

    Declared by the migration itself, as a `-- probe: SELECT ...` comment in
    its header, so the knowledge lives with the migration rather than in a
    table here that the next migration's author would have to remember to
    update. Returns None when a migration declares none.
    """
    head = "\n".join(pathlib.Path(path).read_text(encoding="utf-8").split("\n")[:60])
    match = PROBE_PATTERN.search(head)
    return match.group(1).strip() if match else None


def migration_is_present(conn, path) -> bool:
    """Whether a migration's objects are actually in this database.

    None-ish answers count as absent: a probe that errors (because the table
    it looks at does not exist) or returns NULL means "cannot confirm", and
    the safe direction is to leave the migration pending. Recording it as
    applied when it is not is the failure that hides a feature forever.
    """
    probe = migration_probe(path)
    if probe is None:
        return True              # nothing declared: trust the adopter, as before
    try:
        with conn.cursor() as cur:
            cur.execute(probe)
            row = cur.fetchone()
            return bool(row and row[0])
    except psycopg2.Error:
        conn.rollback()
        return False


def mark_existing_as_applied(conn) -> dict:
    """Adopts a pre-existing database: records migration files as applied
    WITHOUT running them. For the databases that predate migration tracking,
    whose objects are already there.

    This is a claim about history that cannot be verified from inside the
    database, so it is a separate, explicit action rather than something
    apply_schema() infers -- and it refuses to run on a database that has no
    FrameCheck tables at all, where the claim would be plainly false.

    What it will NOT claim: a migration whose own probe says its work is
    absent. Adopting used to record every file, which was fine while every
    migration predated tracking -- and became a bug the moment a NEW one
    existed: adopting a working database would have marked migration 009 as
    applied, reported "migrations up to date", and left the audit trail it
    installs permanently missing. Those are returned as `pending` instead, so
    the next click actually applies them.
    """
    state = schema_state(conn)
    if not state["schema_applied"]:
        raise RuntimeError("this database has no FrameCheck tables, so there is nothing to adopt")
    with conn:
        _ensure_tracking_table(conn)
    adopted, pending = [], []
    for path in migration_files():
        if migration_is_present(conn, path):
            adopted.append(path.name)
        else:
            pending.append(path.name)
    with conn:
        for name in adopted:
            _record_migration(conn, name)
    log.info("adopted %d migration(s), left %d pending: %s",
             len(adopted), len(pending), ", ".join(pending) or "none")
    return {"adopted": adopted, "pending": pending, "state": schema_state(conn)}


def demo_dataset():
    """The bundled dataset, or None if this build does not carry one -- a
    source checkout that has never run scripts/export_dataset.py has no
    datasets/ directory, and the UI should say so rather than offering a
    button that cannot work."""
    return DEMO_DATASET if DEMO_DATASET.is_file() else None


def describe_dataset(path) -> dict:
    """Manifest plus file size, for showing a user what they are about to
    import before they commit to it."""
    path = pathlib.Path(path)
    manifest = read_dataset_manifest(path)
    return {"path": str(path), "bytes": path.stat().st_size, "manifest": manifest}


def open_dataset(path):
    """Datasets are written plain or gzipped; open either as text."""
    path = pathlib.Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def read_dataset_manifest(path) -> dict:
    """The manifest without reading the whole file -- the UI shows what a
    dataset contains before the user commits to importing it. Raises
    ValueError on anything that is not one of our dataset files, rather than
    half-importing something unrecognised."""
    with open_dataset(path) as fh:
        first = fh.readline().strip()
        if not first.startswith(DATASET_MARKER):
            raise ValueError("not a FrameCheck dataset file (no framecheck-dataset header)")
        second = fh.readline().strip()
    prefix = "-- manifest "
    if not second.startswith(prefix):
        raise ValueError("dataset file has no manifest line")
    try:
        return json.loads(second[len(prefix):])
    except json.JSONDecodeError as e:
        raise ValueError(f"dataset manifest is not valid JSON: {e}") from None


def parse_copy_header(line: str):
    """`COPY public.wallet (a, b, c) FROM stdin;` -> ("wallet", ["a","b","c"]).
    Returns None for any other line. Strict on purpose: the table name and
    the column list are the only parts of a dataset file that become SQL, so
    they are matched by pattern and then checked against DATASET_TABLES and
    the live catalogue -- never passed through."""
    match = re.fullmatch(
        r"COPY\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s+FROM\s+stdin;",
        line.strip())
    if not match:
        return None
    table = match.group(1)
    columns = [c.strip() for c in match.group(2).split(",") if c.strip()]
    if not columns or not all(is_safe_identifier(c) for c in columns):
        raise ValueError(f"dataset has an unusable column list for {table}")
    return table, columns


def dataset_target_rows(conn, tables) -> dict:
    """How many rows the target tables already hold. Importing on top of
    existing rows is refused rather than resolved by deleting them."""
    counts = {}
    with conn.cursor() as cur:
        for table in sorted(tables):
            cur.execute("""SELECT 1 FROM information_schema.tables
                            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                              AND table_name = %s""", (table,))
            if cur.fetchone() is None:
                continue
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            count = cur.fetchone()[0]
            if count:
                counts[table] = count
    return counts


def reset_sequences(conn, tables) -> list:
    """After a COPY the serial sequences still sit at 1, so the next INSERT
    would collide with an imported id. Realigns every sequence owned by a
    column of the imported tables.

    Generic rather than a hard-coded list: the schema has serial primary keys
    on a dozen tables and gains more over time, and a missed one does not
    fail here -- it fails later, confusingly, on the first write after an
    import.
    """
    fixed = []
    with conn.cursor() as cur:
        for table in sorted(tables):
            cur.execute("""SELECT column_name FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = %s
                              AND column_default LIKE 'nextval%%'""", (table,))
            for (column,) in cur.fetchall():
                cur.execute(
                    sql.SQL("SELECT setval(pg_get_serial_sequence(%s, %s), "
                            "coalesce((SELECT max({}) FROM {}), 1), "
                            "(SELECT max({}) IS NOT NULL FROM {}))").format(
                        sql.Identifier(column), sql.Identifier(table),
                        sql.Identifier(column), sql.Identifier(table)),
                    (table, column))
                fixed.append(f"{table}.{column}")
    return fixed


def _read_copy_block(fh):
    """Reads one COPY block's data lines, up to and including the `\\.`
    terminator. Returns the lines without it."""
    lines = []
    for line in fh:
        if line.rstrip("\r\n") == "\\.":
            break
        lines.append(line)
    return lines


def _seed_id_map(dataset_rows, columns, seed_table, target_by_name) -> dict:
    """old id -> the id the TARGET database uses for the same name.

    A name the target does not have maps to nothing, and the row referencing
    it would fail its foreign key -- which is the correct outcome: silently
    dropping an analysis run because its algorithm is unknown here would make
    the imported data quietly incomplete.
    """
    id_col, name_col = SEED_KEY[seed_table]
    if id_col not in columns or name_col not in columns:
        return {}
    id_at, name_at = columns.index(id_col), columns.index(name_col)
    mapping = {}
    for line in dataset_rows:
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) <= max(id_at, name_at):
            continue
        old_id, name = fields[id_at], fields[name_at]
        new_id = target_by_name.get(name)
        if new_id is not None and str(new_id) != old_id:
            mapping[old_id] = str(new_id)
    return mapping


def _target_seed_names(conn, seed_table) -> dict:
    id_col, name_col = SEED_KEY[seed_table]
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT {}, {} FROM {}").format(
            sql.Identifier(name_col), sql.Identifier(id_col), sql.Identifier(seed_table)))
        return {row[0]: row[1] for row in cur.fetchall()}


def _remap_lines(lines, columns, remaps) -> tuple:
    """Rewrites the seed-referencing columns of each row. Returns the lines
    and how many fields were changed, so the caller can report it rather than
    doing it invisibly."""
    positions = {}
    for column, seed_table in remaps.items():
        if column in columns:
            positions[columns.index(column)] = seed_table
    if not positions:
        return lines, 0
    changed = 0
    out = []
    for line in lines:
        ending = ""
        body = line
        while body and body[-1] in "\r\n":
            ending = body[-1] + ending
            body = body[:-1]
        fields = body.split("\t")
        for at, seed_table in positions.items():
            if at >= len(fields) or fields[at] == COPY_NULL:
                continue
            mapped = SEED_ID_MAPS.get(seed_table, {}).get(fields[at])
            if mapped is not None:
                fields[at] = mapped
                changed += 1
        out.append("\t".join(fields) + ending)
    return out, changed


# Populated per import; module-level only so _remap_lines can stay a pure
# function of its arguments plus this, rather than threading a dict through
# every call.
SEED_ID_MAPS: dict = {}


def import_dataset(conn, path, progress=None) -> dict:
    """Loads a dataset file produced by scripts/export_dataset.py.

    One transaction: a dataset is only meaningful whole -- half of it would
    leave foreign keys pointing at rows that never arrived -- so a failure
    anywhere rolls the lot back.

    Refuses a database that already holds rows in any of the dataset's own
    tables (seed tables excepted, see SEED_TABLES). The alternative would be
    deleting them first, and this project does not delete a user's analysis to
    make an import convenient.

    Seed tables are kept as the target has them and the dataset's ids are
    translated to the target's by name -- see SEED_KEY for why that is not
    optional.
    """
    global SEED_ID_MAPS
    manifest = read_dataset_manifest(path)
    tables_in_file = [t for t, n in (manifest.get("row_counts") or {}).items() if n]
    checked = [t for t in (tables_in_file or DATASET_TABLES) if t not in SEED_TABLES]
    occupied = dataset_target_rows(conn, checked)
    if occupied:
        summary = ", ".join(f"{t} ({n:,} rows)" for t, n in list(occupied.items())[:4])
        raise RuntimeError(
            f"this database already holds data: {summary}. Importing would collide with it, and "
            "FrameCheck will not delete your rows to make room -- import into an empty database "
            "(create a new one in Connection above) instead.")

    already_seeded = dataset_target_rows(conn, SEED_TABLES)
    SEED_ID_MAPS = {}
    loaded, skipped, remapped = {}, {}, {}
    with conn:                      # commits on success, rolls back on error
        with conn.cursor() as cur:
            with open_dataset(path) as fh:
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if not line.startswith("COPY "):
                        continue
                    header = parse_copy_header(line)
                    if header is None:
                        continue
                    table, columns = header
                    if table not in DATASET_TABLES:
                        raise RuntimeError(
                            f"dataset wants to write to {table!r}, which is not an importable table")

                    rows = _read_copy_block(fh)

                    if table in SEED_TABLES:
                        if table in SEED_KEY:
                            SEED_ID_MAPS[table] = _seed_id_map(
                                rows, columns, table, _target_seed_names(conn, table))
                        if already_seeded.get(table):
                            # Keep the target's own seed rows; the dataset's
                            # references to them are translated instead.
                            skipped[table] = already_seeded[table]
                            continue

                    if table in SEED_REFERENCES:
                        rows, changed = _remap_lines(rows, columns, SEED_REFERENCES[table])
                        if changed:
                            remapped[table] = changed

                    buf = io.StringIO("".join(rows))
                    cur.copy_expert(
                        sql.SQL("COPY {} ({}) FROM STDIN").format(
                            sql.Identifier(table),
                            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                        ).as_string(cur), buf)
                    loaded[table] = len(rows)
                    if progress:
                        progress(table, len(rows))
        sequences = reset_sequences(conn, loaded)

    # ANALYZE straight after, so the Database view's row counts mean something
    # immediately. They come from pg_class.reltuples, which a never-analysed
    # table reports as -1, so without this every freshly imported table reads
    # "unknown" until autovacuum gets round to it -- which looked, in testing,
    # exactly like an import that had not worked.
    with conn.cursor() as cur:
        for table in sorted(loaded):
            cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
    conn.commit()

    # Materialized views are not filled by COPY. mv_cluster_flag_stats
    # (migration 008) is created empty, so straight after an import
    # /clusters/top-flagged returned [] and Multiview showed eight blank
    # panels on a database that held 84 clusters with flagged wallets --
    # the shipped demo data would have looked broken on first use.
    #
    # Plain REFRESH, not CONCURRENTLY: concurrent refresh requires the view
    # to have been populated at least once, which is exactly what is not true
    # here, and nothing else is reading this database mid-import anyway.
    refreshed = []
    with conn.cursor() as cur:
        cur.execute("SELECT matviewname FROM pg_matviews WHERE schemaname = 'public' ORDER BY matviewname")
        for (view,) in cur.fetchall():
            cur.execute(sql.SQL("REFRESH MATERIALIZED VIEW {}").format(sql.Identifier(view)))
            refreshed.append(view)
    conn.commit()

    log.info("imported dataset %s: %d tables, %d rows (%d seed tables kept, %d id fields remapped)",
             path, len(loaded), sum(loaded.values()), len(skipped), sum(remapped.values()))
    return {"manifest": manifest, "loaded": loaded, "skipped_seed_tables": skipped,
            "remapped_ids": remapped, "total_rows": sum(loaded.values()),
            "sequences_reset": len(sequences), "views_refreshed": refreshed}


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
