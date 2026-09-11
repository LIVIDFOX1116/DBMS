"""
Desktop-app packaging, Part 1: the module PyInstaller freezes into the
release-mode backend sidecar (see src-tauri/src/backend.rs's
resolve_backend_command -- dev mode runs `python -m uvicorn api.main:app`
directly against the source tree instead and never touches this file at
all). A frozen exe can't take a `-m module:attr` invocation the way the
interpreter can, so this gives PyInstaller one concrete script to freeze
that imports api.main and starts serving programmatically.

Not meant to be run by a human -- scripts/build_backend_sidecar.py invokes
it as PyInstaller's entry point, and src-tauri spawns the frozen result
with the port as argv[1] and the key/token as env vars (identical to how
the dev path passes them, see spawn_backend in backend.rs).
"""
import sys

import uvicorn

from api.main import app


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: txgraph-backend <port>", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1])
    # log_config=None: uvicorn's default dictConfig setup touches logging
    # handlers PyInstaller's frozen environment doesn't need and has, in
    # other projects, warned about on frozen stdout -- plain root-logger
    # output is all backend.rs's drain_into_ring reads anyway.
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)


if __name__ == "__main__":
    main()
