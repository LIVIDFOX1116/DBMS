#!/usr/bin/env python
"""One command to wipe and reseed: `python demo_schemas/reset_demo.py`.
Instant recovery if something breaks mid-demo -- drops `demo` schema cascade
(FrameCheck's own `public` tables are untouched) and reseeds from scratch.
Thin wrapper around `demo_db.py reset`, kept as its own file for the same
muscle-memory reason as seed_demo.py.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, str(HERE / "demo_db.py"), "reset"]))
