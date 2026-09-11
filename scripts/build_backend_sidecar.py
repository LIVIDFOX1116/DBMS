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

Output goes to src-tauri/binaries/txgraph-backend-<target-triple>.exe --
that exact name (Tauri's externalBin convention: <name>-<host-triple>)
is what tauri.conf.json's bundle.externalBin and backend.rs's release-mode
sidecar resolution both expect. The triple is read from `rustc -vV` rather
than hardcoded so this keeps working if it's ever built on arm64 Windows.
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BINARIES_DIR = REPO_ROOT / "src-tauri" / "binaries"
SIDECAR_NAME = "txgraph-backend"

# api/main.py's only non-code dependency at runtime (see MOCK_PATH there).
# Bundled as api/mock_data.json inside the frozen archive so Path(__file__)
# .parent still finds it at PyInstaller's extraction dir -- see
# backend_entrypoint.py's module docstring.
MOCK_DATA = REPO_ROOT / "api" / "mock_data.json"


def host_target_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"could not find 'host:' line in `rustc -vV` output:\n{out}")


def main() -> None:
    if not MOCK_DATA.exists():
        sys.exit(f"missing {MOCK_DATA} -- run this from a checked-out repo, not a stripped copy")

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
        # uvicorn/starlette pick their event-loop and protocol implementations
        # dynamically at import time (uvicorn.loops.auto, .protocols.http.auto,
        # ...) -- PyInstaller's static import scan misses those, so the frozen
        # exe raises ImportError on first request without --collect-all here.
        "--collect-all", "uvicorn",
        "--collect-all", "starlette",
        str(REPO_ROOT / "scripts" / "backend_entrypoint.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    shutil.rmtree(build_dir, ignore_errors=True)

    built = BINARIES_DIR / (exe_name + (".exe" if sys.platform == "win32" else ""))
    if not built.exists():
        sys.exit(f"PyInstaller reported success but {built} is missing -- check its output above")
    print(f"\nBuilt {built} ({built.stat().st_size / 1_000_000:.1f} MB)")
    print("Verify it standalone before trusting it in a release build:")
    print(f"  {built} 8765   # then in another terminal: curl http://127.0.0.1:8765/health")


if __name__ == "__main__":
    main()
