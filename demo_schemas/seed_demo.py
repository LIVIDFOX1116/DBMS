#!/usr/bin/env python
"""One command to load every demo schema: `python demo_schemas/seed_demo.py`.
Thin wrapper around `demo_db.py seed` -- kept as its own file because that is
the exact name asked for, so it is what muscle memory reaches for on demo day.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, str(HERE / "demo_db.py"), "seed"]))
