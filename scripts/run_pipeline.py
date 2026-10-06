"""
Run the whole pipeline -- ingestion, snapshot export, PageRank+Louvain+BFS
analysis, wallet risk scoring, FrameCheck detectors, evidence collection,
and the audit engine -- as one command, zero manual hand-off between stages.

    python scripts/run_pipeline.py                         # every stage, fresh
    python scripts/run_pipeline.py --skip-ingestion         # reuse what's already in Postgres
    python scripts/run_pipeline.py --snapshot-id 4          # reuse an existing snapshot (implies analyze onward)
    python scripts/run_pipeline.py --from-stage detect --until-stage audit --snapshot-id 4
    python scripts/run_pipeline.py --hops 1 --force-cpu

Stages, in order: ingest, export, analyze, score, detect, evidence, audit.
--from-stage/--until-stage select a contiguous sub-range (both inclusive);
each stage is the exact same `python -m ...` command documented in
README.md / CLAUDE.md -- this only removes the manual hand-off between
them. Ingestion is upsert-based and safe to re-run; export always creates
a new, uniquely-named snapshot rather than overwriting one.
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
STAGES = ["ingest", "export", "analyze", "score", "detect", "evidence", "audit"]


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
                    "something upstream failed silently, or --skip-ingestion/--snapshot-id was "
                    "passed against a database that's never been ingested."
                )
            return row[0]
    finally:
        conn.close()


def resolve_latest_snapshot_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_id FROM graph_snapshot ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise SystemExit("No graph_snapshot rows found -- run the export stage first, or pass --snapshot-id.")
        return row[0]


def default_snapshot_name() -> str:
    return f"pipeline_{time.strftime('%Y%m%dT%H%M%S')}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default=config.CASE_STUDY_ADDRESS)
    parser.add_argument("--hops", type=int, default=config.HOP_COUNT)
    parser.add_argument("--snapshot-name", default=None,
                         help="defaults to a timestamped name (graph_snapshot.snapshot_name must be unique)")
    parser.add_argument("--snapshot-id", type=int, default=None,
                         help="reuse this existing snapshot instead of ingesting+exporting a new one "
                              "-- implies --from-stage analyze unless --from-stage is given explicitly")
    parser.add_argument("--skip-ingestion", action="store_true",
                         help="reuse whatever's already in Postgres instead of crawling Etherscan again")
    parser.add_argument("--force-cpu", action="store_true", help="skip RAPIDS even if installed, for graph/run_analysis.py")
    parser.add_argument("--from-stage", choices=STAGES, default=None)
    parser.add_argument("--until-stage", choices=STAGES, default=None)
    parser.add_argument("--audit-limit", type=int, default=None, help="passthrough to graph.audit's --limit")
    args = parser.parse_args()

    from_stage = args.from_stage
    if from_stage is None:
        if args.snapshot_id is not None:
            from_stage = "analyze"
        elif args.skip_ingestion:
            from_stage = "export"
        else:
            from_stage = "ingest"
    until_stage = args.until_stage or "audit"

    from_idx, until_idx = STAGES.index(from_stage), STAGES.index(until_stage)
    if from_idx > until_idx:
        raise SystemExit(f"--from-stage {from_stage} comes after --until-stage {until_stage}")

    def enabled(name):
        return from_idx <= STAGES.index(name) <= until_idx

    t_start = time.time()
    snapshot_name = args.snapshot_name or default_snapshot_name()

    print("=" * 70)
    print(f"End-to-end pipeline run: {' -> '.join(STAGES[from_idx:until_idx + 1])}")
    print("=" * 70)

    if enabled("ingest"):
        print(f"\n[ingest] Ingesting {args.hops}-hop subgraph around {args.address}...")
        run([sys.executable, "-m", "ingestion.ingest_subgraph", "--address", args.address, "--hops", str(args.hops)])
    else:
        print("\n[ingest] skipped")

    snapshot_id = args.snapshot_id
    if enabled("export"):
        print(f"\n[export] Exporting snapshot '{snapshot_name}'...")
        export_result = run(
            [sys.executable, "-m", "graph.export_snapshot", "--name", snapshot_name,
             "--start-block", "0", "--end-block", "999999999"],
            capture_output=True, text=True,
        )
        print(export_result.stdout.strip())
        snapshot_id = parse_snapshot_id(export_result.stdout)
    else:
        print("\n[export] skipped")
        if snapshot_id is None:
            conn = psycopg2.connect(**config.PG_DSN)
            try:
                snapshot_id = resolve_latest_snapshot_id(conn)
            finally:
                conn.close()
        print(f"  using existing snapshot_id={snapshot_id}")

    seed_wallet_id = resolve_seed_wallet_id(args.address)
    print(f"  seed_wallet_id={seed_wallet_id}")

    if enabled("analyze"):
        print(f"\n[analyze] Running PageRank + Louvain + BFS (snapshot_id={snapshot_id})...")
        analysis_cmd = [sys.executable, "-m", "graph.run_analysis",
                         "--snapshot-id", str(snapshot_id), "--seed-wallet-id", str(seed_wallet_id)]
        if args.force_cpu:
            analysis_cmd.append("--force-cpu")
        run(analysis_cmd)
    else:
        print("\n[analyze] skipped")

    if enabled("score"):
        print(f"\n[score] Scoring wallet.risk_score (snapshot_id={snapshot_id})...")
        run([sys.executable, "-m", "graph.score_wallets", "--snapshot-id", str(snapshot_id)])
    else:
        print("\n[score] skipped")

    if enabled("detect"):
        print(f"\n[detect] Running FrameCheck detectors (snapshot_id={snapshot_id})...")
        run([sys.executable, "-m", "graph.detectors",
             "--snapshot-id", str(snapshot_id), "--seed-wallet-id", str(seed_wallet_id), "--detector", "all"])
    else:
        print("\n[detect] skipped")

    if enabled("evidence"):
        print(f"\n[evidence] Collecting flag evidence (snapshot_id={snapshot_id})...")
        run([sys.executable, "-m", "graph.evidence",
             "--snapshot-id", str(snapshot_id), "--seed-wallet-id", str(seed_wallet_id)])
    else:
        print("\n[evidence] skipped")

    if enabled("audit"):
        print(f"\n[audit] Running the audit engine (snapshot_id={snapshot_id})...")
        audit_cmd = [sys.executable, "-m", "graph.audit",
                     "--snapshot-id", str(snapshot_id), "--seed-wallet-id", str(seed_wallet_id)]
        if args.audit_limit is not None:
            audit_cmd += ["--limit", str(args.audit_limit)]
        run(audit_cmd)
    else:
        print("\n[audit] skipped")

    elapsed = time.time() - t_start
    print("\n" + "=" * 70)
    print(f"Pipeline complete in {elapsed:.1f}s -- snapshot_id={snapshot_id}, seed_wallet_id={seed_wallet_id}")
    print("View it: TXGRAPH_API_MODE=live uvicorn api.main:app --port 8000, "
          "then open frontend/index.html (Settings -> run ID -> the analysis_run "
          "id(s) this just created).")
    print("=" * 70)


if __name__ == "__main__":
    main()
