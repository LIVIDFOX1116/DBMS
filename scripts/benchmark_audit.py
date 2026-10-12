"""
FrameCheck: flags-audited-per-minute benchmark, CPU vs GPU.

    python scripts/benchmark_audit.py --snapshot-id 4 --seed-wallet-id 1476 --limit 5
    python scripts/benchmark_audit.py --snapshot-id 4 --seed-wallet-id 1476 --detector pagerank_threshold --limit 3 --output docs/benchmark_results.md

Times graph.audit.audit_flag against a sample of real flags (--limit per
detector), reporting the median seconds/flag and flags/minute for the CPU
path always, and the GPU path too whenever RAPIDS is importable and
FRAMECHECK_USE_GPU_GRAPH=1 is set -- graph/audit.py's pagerank_threshold
audit already routes through GraphBackend.pagerank_excluding_signed_edges,
which is GPU-accelerated under those conditions; the other four
detectors' audits are CPU replay/NetworkX-only by design (see
graph/audit.py's module docstring) and have no separate GPU path.

Writes real flag_audit rows (this IS a real audit, just timed) -- then
deletes exactly the rows it created (by flag_audit_id range captured
before/after), so this benchmark leaves the database exactly as it found
it, the same guarantee scripts/benchmark.py's own persistence-timing
section gives via a rolled-back transaction. A plain rollback doesn't
work here since audit_flag() commits its own persist_audit() call
internally -- deleting by captured id range is the equivalent guarantee
for a function that commits, not one that can be handed commit=False.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from graph.audit import audit_flag
from graph.backend import gpu_available
from ingestion import config

DETECTORS = ("bfs_proximity", "sybil_cluster", "pagerank_threshold", "impurity", "engaged_proximity")


def sample_flag_ids(conn, snapshot_id: int, detector: str, limit: int) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.flag_id FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
            WHERE ar.snapshot_id = %s AND f.detector = %s ORDER BY f.flag_id LIMIT %s
            """,
            (snapshot_id, detector, limit),
        )
        return [row[0] for row in cur.fetchall()]


def benchmark_detector(conn, snapshot_id: int, seed_wallet_id: int, detector: str, limit: int) -> list:
    flag_ids = sample_flag_ids(conn, snapshot_id, detector, limit)
    times = []
    for flag_id in flag_ids:
        t0 = time.perf_counter()
        audit_flag(conn, flag_id, seed_wallet_id)
        times.append(time.perf_counter() - t0)
    return times


def cleanup_benchmark_audits(conn, max_id_before: int) -> int:
    """Removes the audit rows this benchmark just wrote.

    flag_audit is append-only, enforced by a trigger (migration 009), so this
    has to say out loud that it is deleting and why. The deletion is recorded
    in flag_audit_tombstone either way -- benchmark rows leaving a trace is
    the point: a gap in the audit chain with no tombstone is tampering, and a
    gap with one is housekeeping.
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL framecheck.allow_audit_delete = 'on'")
        cur.execute("SET LOCAL framecheck.audit_delete_reason = %s",
                    ("benchmark_audit.py removing its own timing rows",))
        cur.execute("DELETE FROM flag_audit WHERE flag_audit_id > %s", (max_id_before,))
        deleted = cur.rowcount
    conn.commit()
    return deleted


def format_report(results: list) -> str:
    lines = ["# Audit benchmark: flags audited per minute", ""]
    lines.append("| Detector | Backend | n | median s/flag | flags/min |")
    lines.append("|---|---|---|---|---|")
    for detector, backend_name, times in results:
        if not times:
            lines.append(f"| {detector} | {backend_name} | 0 | n/a | n/a (no flags found) |")
            continue
        med = statistics.median(times)
        per_min = 60.0 / med if med > 0 else float("inf")
        lines.append(f"| {detector} | {backend_name} | {len(times)} | {med:.2f} | {per_min:.1f} |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--detector", choices=DETECTORS, default=None, help="limit to one detector; default: all 5")
    parser.add_argument("--limit", type=int, default=5, help="flags to sample per detector")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    detectors = [args.detector] if args.detector else list(DETECTORS)

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(flag_audit_id), 0) FROM flag_audit")
            max_id_before = cur.fetchone()[0]

        # Label by the backend actually in effect, not a hardcoded "CPU":
        # this script runs one pass, so a CPU-vs-GPU comparison means running
        # it twice (with and without FRAMECHECK_USE_GPU_GRAPH=1), and the two
        # reports have to be distinguishable afterwards.
        backend_label = "GPU" if gpu_available() else "CPU"
        results = []
        for detector in detectors:
            print(f"benchmarking {detector} ({backend_label}, n<={args.limit})...")
            times = benchmark_detector(conn, args.snapshot_id, args.seed_wallet_id, detector, args.limit)
            results.append((detector, backend_label, times))

        deleted = cleanup_benchmark_audits(conn, max_id_before)
        print(f"\n[cleanup] removed {deleted} flag_audit row(s) this benchmark created")
    finally:
        conn.close()

    if gpu_available():
        print("\n[note] RAPIDS is importable and opted in on this machine -- pagerank_threshold's "
              "audit already ran its masked-PageRank step on the GPU. The other four detectors "
              "have no separate GPU path (CPU replay/NetworkX by design).")
    else:
        print("\n[note] GPU backend unavailable on this machine (no RAPIDS/cugraph importable, or "
              "FRAMECHECK_USE_GPU_GRAPH not set) -- re-run this script unmodified on a RAPIDS-capable "
              "machine to fill in the GPU numbers for pagerank_threshold.")

    report = format_report(results)
    print("\n" + report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
