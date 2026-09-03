"""
Phase 6 in the project bible: run the whole pipeline -- ingestion, snapshot
export, and PageRank+Louvain+BFS analysis -- as one command. Exit criterion:
"A fresh end-to-end run (ingestion -> GPU processing -> dashboard) completes
without manual intervention."

Before this script, that hand-off wasn't actually manual-intervention-free:
README.md's own instructions have you run a `psql` query by hand to look up
a wallet_id and paste it into the next command. This script does that lookup
itself, and reads the snapshot_id straight out of export_snapshot's output
instead of you copying it in.

    python scripts/run_pipeline.py                       # ingest + export + analyze, fresh
    python scripts/run_pipeline.py --skip-ingestion       # reuse what's already in Postgres
    python scripts/run_pipeline.py --hops 1 --force-cpu

Each stage is the exact same `python -m ...` command documented in
README.md -- this only removes the manual hand-off between them, it doesn't
change what any stage does or does to your data (ingestion is upsert-based
and safe to re-run; export always creates a new, uniquely-named snapshot
rather than overwriting one, matching the "every run is reproducible and
comparable, never overwritten in place" rule from the ER diagram notes).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Without this, this script's own print()s (buffered) and each subprocess
# call's inherited-stdout output (unbuffered) interleave out of actual
# execution order -- confusing when a stage's own log appears to print
# before the "starting stage" line that triggered it.
sys.stdout.reconfigure(line_buffering=True)

import psycopg2

from ingestion import config

SNAPSHOT_ID_RE = re.compile(r"snapshot_id=(\d+)")


def run(cmd, **kwargs):
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"[ABORT] stage failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def parse_snapshot_id(export_stdout: str) -> int:
    """Pulls the snapshot_id out of `snapshot_id=<N> written to <path>`,
    graph/export_snapshot.py's own stdout line -- the thing you'd otherwise
    have to read off the terminal and type into the next command by hand."""
    match = SNAPSHOT_ID_RE.search(export_stdout)
    if not match:
        raise SystemExit(f"Could not find snapshot_id in export_snapshot output: {export_stdout!r}")
    return int(match.group(1))


def resolve_seed_wallet_id(address: str) -> int:
    conn = psycopg2.connect(**config.PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT wallet_id FROM wallet WHERE address = lower(%s)", (address,))
            row = cur.fetchone()
            if row is None:
                raise SystemExit(
                    f"Seed address {address} not found in wallet table after ingestion -- "
                    "something upstream failed silently, or --skip-ingestion was passed "
                    "against a database that's never been ingested."
                )
            return row[0]
    finally:
        conn.close()


def default_snapshot_name() -> str:
    return f"pipeline_{time.strftime('%Y%m%dT%H%M%S')}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default=config.CASE_STUDY_ADDRESS)
    parser.add_argument("--hops", type=int, default=config.HOP_COUNT)
    parser.add_argument("--snapshot-name", default=None,
                         help="defaults to a timestamped name (graph_snapshot.snapshot_name must be unique)")
    parser.add_argument("--skip-ingestion", action="store_true",
                         help="reuse whatever's already in Postgres instead of crawling Etherscan again")
    parser.add_argument("--force-cpu", action="store_true", help="skip RAPIDS even if installed, for graph/run_analysis.py")
    args = parser.parse_args()

    t_start = time.time()
    snapshot_name = args.snapshot_name or default_snapshot_name()

    print("=" * 70)
    print("Phase 6: end-to-end pipeline run -- zero manual steps")
    print("=" * 70)

    if args.skip_ingestion:
        print(f"\n[1/3] Skipping ingestion (--skip-ingestion) -- using whatever's already "
              f"in Postgres for {args.address}")
    else:
        print(f"\n[1/3] Ingesting {args.hops}-hop subgraph around {args.address}...")
        run([sys.executable, "-m", "ingestion.ingest_subgraph", "--address", args.address, "--hops", str(args.hops)])

    print(f"\n[2/3] Exporting snapshot '{snapshot_name}'...")
    export_result = run(
        [sys.executable, "-m", "graph.export_snapshot", "--name", snapshot_name,
         "--start-block", "0", "--end-block", "999999999"],
        capture_output=True, text=True,
    )
    print(export_result.stdout.strip())
    snapshot_id = parse_snapshot_id(export_result.stdout)

    seed_wallet_id = resolve_seed_wallet_id(args.address)
    print(f"\n[3/3] Running PageRank + Louvain + BFS (snapshot_id={snapshot_id}, seed_wallet_id={seed_wallet_id})...")
    analysis_cmd = [sys.executable, "-m", "graph.run_analysis",
                     "--snapshot-id", str(snapshot_id), "--seed-wallet-id", str(seed_wallet_id)]
    if args.force_cpu:
        analysis_cmd.append("--force-cpu")
    run(analysis_cmd)

    elapsed = time.time() - t_start
    print("\n" + "=" * 70)
    print(f"Pipeline complete in {elapsed:.1f}s -- snapshot_id={snapshot_id}, seed_wallet_id={seed_wallet_id}")
    print("View it: TXGRAPH_API_MODE=live uvicorn api.main:app --port 8000, "
          "then open frontend/index.html (Settings -> run ID -> the analysis_run "
          "id(s) this just created).")
    print("=" * 70)


if __name__ == "__main__":
    main()
