"""Backup and verified restore with PostgreSQL's native tools (pg_dump /
pg_restore). The logic lives here so it can be unit-tested offline; the
shell wrappers (scripts/backup_db.{sh,ps1}, scripts/restore_db.{sh,ps1}) only
find a Python and call this.

    python scripts/dbtools.py backup  [--dbname framecheck] [--out-dir backups]
    python scripts/dbtools.py restore backups/framecheck_<ts>.dump [--target NAME]

Credentials come from the same place as everything else in the repo
(PG* in the environment / .env, via ingestion.config) and reach pg_dump and
pg_restore ONLY through their environment -- never on the command line,
where any local process could read them -- or through the user's own
.pgpass. -w (--no-password) makes the tools fail rather than prompt.

A restore never lands on top of an existing database by accident: it goes
into a NEW database (framecheck_restore_check by default), and restoring
into any database that already exists needs --target plus typing that
database's name to confirm. Afterwards it compares exact row counts table by
table against the source and runs flag_audit_chain_verify() on the copy.
"""
# RAW SQL (allowed): backup/restore verification -- catalogue row counts and the PL/pgSQL chain verifier, identifiers quoted with sql.Identifier
import argparse
import glob
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_RESTORE_TARGET = "framecheck_restore_check"


# ------------------------------------------------------------------ pure parts

def find_pg_tool(name: str) -> str:
    """pg_dump / pg_restore: $PG_BIN first, then PATH, then the newest
    Windows install under Program Files."""
    exe = name + (".exe" if os.name == "nt" else "")
    if os.getenv("PG_BIN"):
        candidate = Path(os.environ["PG_BIN"]) / exe
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    installs = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\\" + exe),
                      key=lambda p: int(Path(p).parent.parent.name) if Path(p).parent.parent.name.isdigit() else 0)
    if installs:
        return installs[-1]
    raise SystemExit(f"{name} not found -- install the PostgreSQL client tools or set PG_BIN")


def tool_env(dsn: dict, dbname: str | None = None) -> dict:
    """The child process environment: the parent's, plus libpq's PG*
    variables. The password travels here and nowhere else."""
    from api import dbsetup

    env = dict(os.environ)
    host = dbsetup.connect_order(dsn.get("host"))[0]      # 127.0.0.1 before `localhost` (~2 s each on Windows)
    env.update({"PGHOST": host, "PGPORT": str(dsn.get("port") or 5432),
                "PGUSER": str(dsn.get("user") or ""), "PGDATABASE": dbname or dsn["dbname"]})
    if dsn.get("password"):
        env["PGPASSWORD"] = str(dsn["password"])
    return env


def backup_command(pg_dump: str, dbname: str, outfile: Path) -> list[str]:
    # -Fc: custom format -- compressed, and pg_restore can restore it selectively.
    return [pg_dump, "-w", "-Fc", "-d", dbname, "-f", str(outfile)]


def restore_command(pg_restore: str, target: str, dumpfile: Path) -> list[str]:
    # --no-owner: the copy is owned by whoever restores it, so a dump taken
    # as one role restores cleanly as another (e.g. into a managed cloud DB).
    return [pg_restore, "-w", "--no-owner", "--exit-on-error", "-d", target, str(dumpfile)]


def backup_filename(dbname: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{dbname}_{stamp}.dump"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_identifier(name: str) -> str:
    from api import dbsetup

    if not dbsetup.is_safe_identifier(name):
        raise SystemExit(f"refusing database name {name!r}: letters, digits and underscores only")
    return name


def confirm_overwrite(target: str, answer: str) -> bool:
    """Restoring into an existing database needs its name typed back exactly."""
    return answer.strip() == target


# ------------------------------------------------------------- database parts

def database_exists(dsn: dict, name: str) -> bool:
    from api import dbsetup

    conn = dbsetup.connect(dsn, dbname="postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def exact_counts(dsn: dict, dbname: str) -> dict:
    from psycopg2 import sql

    from api import dbsetup

    conn = dbsetup.connect(dsn, dbname=dbname)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                           WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1""")
            tables = [r[0] for r in cur.fetchall()]
            counts = {}
            for table in tables:
                cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
                counts[table] = cur.fetchone()[0]
            return counts
    finally:
        conn.close()


def chain_verify(dsn: dict, dbname: str):
    from api import dbsetup

    conn = dbsetup.connect(dsn, dbname=dbname)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regprocedure('flag_audit_chain_verify()') IS NOT NULL")
            if not cur.fetchone()[0]:
                return None
            cur.execute("SELECT rows_checked, intact, first_bad_audit_id, reason FROM flag_audit_chain_verify()")
            return cur.fetchone()
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------- CLI

def cmd_backup(args) -> int:
    from api import dbsetup

    dsn = dbsetup.current_dsn()
    dbname = check_identifier(args.dbname or dsn["dbname"])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outfile = out_dir / backup_filename(dbname)
    print(f"pg_dump -Fc {dbname} -> {outfile}")
    subprocess.run(backup_command(find_pg_tool("pg_dump"), dbname, outfile), env=tool_env(dsn, dbname), check=True)
    print(f"size   {outfile.stat().st_size:,} bytes")
    print(f"sha256 {sha256_of(outfile)}")
    return 0


def cmd_restore(args) -> int:
    from api import dbsetup

    dsn = dbsetup.current_dsn()
    dumpfile = Path(args.dumpfile)
    if not dumpfile.exists():
        raise SystemExit(f"{dumpfile} does not exist")
    source = check_identifier(args.source or dsn["dbname"])
    target = check_identifier(args.target or DEFAULT_RESTORE_TARGET)

    if database_exists(dsn, target):
        if not args.target:
            raise SystemExit(f"{target} already exists. Restore checks go into a NEW database: drop it yourself "
                             f"once you are done with it, or pass --target with another name.")
        print(f"WARNING: {target} already exists and the restore will write into it.")
        if not confirm_overwrite(target, input(f"Type the database name ({target}) to confirm: ")):
            print("Not confirmed; nothing restored.")
            return 1
    else:
        dbsetup.create_database({**dsn, "dbname": target})
        print(f"created database {target}")

    print(f"sha256 {sha256_of(dumpfile)}  {dumpfile}")
    print(f"pg_restore -> {target}")
    subprocess.run(restore_command(find_pg_tool("pg_restore"), target, dumpfile),
                   env=tool_env(dsn, target), check=True)

    print(f"\nRow counts, {source} (source) vs {target} (restored):")
    want, got = exact_counts(dsn, source), exact_counts(dsn, target)
    ok = True
    for table in sorted(set(want) | set(got)):
        a, b = want.get(table), got.get(table)
        mark = "ok" if a == b else "MISMATCH"
        ok &= a == b
        print(f"  {mark:8} {table:24} {a!s:>10} {b!s:>10}")
    chain = chain_verify(dsn, target)
    if chain is None:
        print("audit chain: flag_audit_chain_verify() not installed in the copy")
    else:
        rows, intact, bad, reason = chain
        print(f"audit chain: {rows} rows checked, intact={intact}" + ("" if intact else f", first bad row {bad}: {reason}"))
        ok &= bool(intact)
    print("\nRESTORE CHECK: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="pg_dump -Fc to backups/<db>_<UTC>.dump")
    b.add_argument("--dbname", help="database to back up (default: PGDATABASE)")
    b.add_argument("--out-dir", default=str(REPO / "backups"))
    r = sub.add_parser("restore", help="pg_restore into a new database and verify it")
    r.add_argument("dumpfile")
    r.add_argument("--target", help=f"database to restore into (default: {DEFAULT_RESTORE_TARGET}, which must not exist)")
    r.add_argument("--source", help="database to compare row counts against (default: PGDATABASE)")
    args = parser.parse_args(argv)
    return cmd_backup(args) if args.command == "backup" else cmd_restore(args)


if __name__ == "__main__":
    sys.exit(main())
