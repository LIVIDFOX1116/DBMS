"""
Desktop-app packaging, Part 1: freezes api/main.py (via
scripts/backend_entrypoint.py) into the single-file backend sidecar
release-mode Tauri builds spawn -- see src-tauri/src/backend.rs's
resolve_backend_command(), which errors on purpose in release mode until
this has been run at least once.

Not part of the normal dev loop (`cargo tauri dev` runs api/main.py
straight from source through .venv-win, see backend.rs) -- run this only
before `cargo build --release` / `cargo tauri build`:

    python scripts/build_backend_sidecar.py

Output goes to src-tauri/binaries/framecheck-backend-<target-triple>.exe --
that exact name (Tauri's externalBin convention: <name>-<host-triple>)
is what tauri.conf.json's bundle.externalBin and backend.rs's release-mode
sidecar resolution both expect. The triple is read from `rustc -vV` rather
than hardcoded so this keeps working if it's ever built on arm64 Windows.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BINARIES_DIR = REPO_ROOT / "src-tauri" / "binaries"
SIDECAR_NAME = "framecheck-backend"

# api/main.py's only non-code dependency at runtime (see MOCK_PATH there).
# Bundled as api/mock_data.json inside the frozen archive so Path(__file__)
# .parent still finds it at PyInstaller's extraction dir -- see
# backend_entrypoint.py's module docstring.
MOCK_DATA = REPO_ROOT / "api" / "mock_data.json"
# api/main.py serves GET /benchmarks straight off this file, resolved as
# Path(__file__).parent.parent / "docs" / "perf" -- which inside the frozen
# archive is the extraction dir, so without bundling it the packaged app's
# Benchmarks tab is permanently empty. Found by running the built sidecar
# standalone before trusting it. The shipped copy is whatever was measured
# at build time; re-benchmarking on a user's machine writes to the repo, not
# into the bundle.
BENCHMARKS_DATA = REPO_ROOT / "docs" / "perf" / "benchmarks.json"
# graph/evidence.py reads this at runtime (QUERY_PATH), resolved relative to
# its own module location -- which in the frozen archive is the extraction
# dir. Without it every audit started from the packaged app dies with
# FileNotFoundError on evidence.sql. Same class of bug as mock_data.json
# above: anything read off disk at runtime has to be bundled.
EVIDENCE_SQL = REPO_ROOT / "db" / "queries" / "evidence.sql"
# Feature 1: the Database view applies the schema to a user's own Postgres and
# can import the demo dataset, so an installed app has to carry the SQL files
# and the dataset -- anything read off disk at runtime must be bundled
# explicitly (the lesson from mock_data.json, benchmarks.json and
# evidence.sql, each of which shipped broken once).
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
DEMO_DATASET = REPO_ROOT / "datasets" / "snapshot1_polynetwork.sql.gz"


def host_target_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"could not find 'host:' line in `rustc -vV` output:\n{out}")


def main() -> None:
    if not MOCK_DATA.exists():
        sys.exit(f"missing {MOCK_DATA} -- run this from a checked-out repo, not a stripped copy")
    if not EVIDENCE_SQL.exists():
        sys.exit(f"missing {EVIDENCE_SQL} -- the audit needs it at runtime")

    triple = host_target_triple()
    exe_name = f"{SIDECAR_NAME}-{triple}"
    build_dir = REPO_ROOT / "build" / "backend_sidecar"  # PyInstaller's scratch dir, not the output

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", exe_name,
        "--distpath", str(BINARIES_DIR),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
        "--paths", str(REPO_ROOT),
        "--add-data", f"{MOCK_DATA}{';' if sys.platform == 'win32' else ':'}api",
        *(("--add-data", f"{BENCHMARKS_DATA}{';' if sys.platform == 'win32' else ':'}docs/perf")
          if BENCHMARKS_DATA.exists() else ()),
        "--add-data", f"{EVIDENCE_SQL}{';' if sys.platform == 'win32' else ':'}db/queries",
        # The schema and migrations: the Database view applies them to a
        # user's own Postgres, so the installed app has to carry the SQL.
        "--add-data", f"{SCHEMA_SQL}{';' if sys.platform == 'win32' else ':'}db",
        "--add-data", f"{MIGRATIONS_DIR}{';' if sys.platform == 'win32' else ':'}db/migrations",
        # The demo dataset, so a fresh install has real data to look at.
        *(("--add-data", f"{DEMO_DATASET}{';' if sys.platform == 'win32' else ':'}datasets")
          if DEMO_DATASET.exists() else ()),
        # uvicorn/starlette pick their event-loop and protocol implementations
        # dynamically at import time (uvicorn.loops.auto, .protocols.http.auto,
        # ...) -- PyInstaller's static import scan misses those, so the frozen
        # exe raises ImportError on first request without --collect-all here.
        "--collect-all", "uvicorn",
        "--collect-all", "starlette",
        str(REPO_ROOT / "scripts" / "backend_entrypoint.py"),
    ]
    # Keep PyInstaller's temporary files beside the repo instead of on the
    # system drive.
    #
    # --workpath/--specpath above already point here, but PyInstaller (and the
    # zip/wheel machinery underneath it) also uses the system temp directory,
    # and that is usually on C:. This build needs a couple of GB there, which
    # is a miserable dependency when the repo itself sits on another volume:
    # the failure is `PyInstaller ... returned non-zero exit status
    # 4294967295` -- no mention of disk space anywhere -- and it happened here
    # with 80 MB free on C: while E: had 58 GB.
    scratch = build_dir / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMP": str(scratch), "TEMP": str(scratch)}

    print("Running:", " ".join(cmd))
    print(f"  (temp files -> {scratch})")
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)

    shutil.rmtree(build_dir, ignore_errors=True)

    built = BINARIES_DIR / (exe_name + (".exe" if sys.platform == "win32" else ""))
    if not built.exists():
        sys.exit(f"PyInstaller reported success but {built} is missing -- check its output above")
    print(f"\nBuilt {built} ({built.stat().st_size / 1_000_000:.1f} MB)")
    print("Verify it standalone before trusting it in a release build:")
    print(f"  {built} 8765   # then in another terminal: curl http://127.0.0.1:8765/health")


if __name__ == "__main__":
    main()
