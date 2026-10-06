"""
FrameCheck: a bounded, time-boxed real audit pass across all five
detectors on one snapshot, CPU only.

    python scripts/full_audit_pass.py --snapshot-id 5 --seed-wallet-id 1476

Scope (matches the live-launch "evidence CTE has a performance cliff for
high-inbound-degree wallets" finding in docs/FRAMECHECK.md -- this script
is how that finding gets worked around for a real full-ish pass instead
of a hand-picked small sample):
  - bfs_proximity: every flagged wallet EXCEPT "known hubs" -- wallets
    whose inbound-transaction count in this snapshot exceeds
    --hub-indegree-cutoff (default 500). These are exactly the wallets
    whose evidence-chain recursive CTE is slow (one backward-walk branch
    per inbound transaction, not per sender -- see graph/audit.py's
    module docstring), so excluding them by indegree is a direct,
    defensible proxy for excluding the slow ones, not an arbitrary cut.
  - impurity, engaged_proximity: every flagged wallet (both currency-aware
    detectors raise few enough flags on real data that no limit is needed).
  - pagerank_threshold: the first --pagerank-limit flags (default 30).
  - sybil_cluster: --sybil-limit flags (default 50), RANDOMLY sampled
    (ORDER BY RANDOM()) rather than the first N by flag_id, since
    sybil_cluster's audit never touches evidence collection at all (see
    the live benchmark in docs/benchmark_results.md) and a random sample
    is a fairer throughput estimate across its ~6,000+ real flags than
    whatever happens to sort first.

Per-flag timeout: SET statement_timeout to --timeout-seconds (default
120) for the whole pass. A flag whose audit_flag() call gets cancelled is
counted as a SKIP (never silently dropped -- the final report states the
skip count per detector), the aborted transaction is rolled back, and the
pass moves on to the next flag.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2 import errors as pg_errors

from ingestion import config
from graph.audit import audit_flag

DEFAULT_HUB_INDEGREE_CUTOFF = 500
DEFAULT_PAGERANK_LIMIT = 30
DEFAULT_SYBIL_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 120


def resolve_latest_run_id(cur, snapshot_id: int, detector: str) -> int:
    cur.execute(
        """
        SELECT ar.run_id FROM analysis_run ar JOIN algorithm al ON al.algorithm_id = ar.algorithm_id
        WHERE ar.snapshot_id = %s AND al.algorithm_name = %s AND ar.status = 'DONE'
        ORDER BY ar.run_id DESC LIMIT 1
        """,
        (snapshot_id, detector),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"no completed '{detector}' run for snapshot {snapshot_id}")
    return row[0]


def select_bfs_proximity_flag_ids(cur, snapshot_id: int, run_id: int, hub_indegree_cutoff: int) -> tuple:
    """Returns (flag_ids_to_audit, excluded_hub_wallet_ids)."""
    cur.execute("SELECT flag_id, wallet_id FROM flag WHERE run_id = %s", (run_id,))
    flag_by_wallet = {wallet_id: flag_id for flag_id, wallet_id in cur.fetchall()}
    wallet_ids = list(flag_by_wallet.keys())
    if not wallet_ids:
        return [], []
    cur.execute(
        """
        SELECT t.to_wallet_id, count(*) FROM snapshot_edge_tx st
        JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        WHERE se.snapshot_id = %s AND t.to_wallet_id = ANY(%s)
        GROUP BY t.to_wallet_id
        """,
        (snapshot_id, wallet_ids),
    )
    indegree = dict(cur.fetchall())
    hubs = [w for w in wallet_ids if indegree.get(w, 0) > hub_indegree_cutoff]
    kept = [w for w in wallet_ids if w not in hubs]
    return [flag_by_wallet[w] for w in kept], hubs


def select_flag_ids(cur, run_id: int, limit: int = None, random_order: bool = False) -> list:
    query = "SELECT flag_id FROM flag WHERE run_id = %s"
    if random_order:
        query += " ORDER BY RANDOM()"
    if limit is not None:
        query += " LIMIT %s"
        cur.execute(query, (run_id, limit))
    else:
        cur.execute(query, (run_id,))
    return [row[0] for row in cur.fetchall()]


def run_audit_pass(conn, flag_ids: list, seed_wallet_id: int, timeout_seconds: int) -> dict:
    """Audits each flag_id in turn under a per-statement timeout. Returns
    {"audited": [(flag_id, verdict, runtime_s), ...], "skipped": [(flag_id, reason), ...]}."""
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(timeout_seconds * 1000)}")
    conn.commit()

    audited = []
    skipped = []
    for flag_id in flag_ids:
        t0 = time.perf_counter()
        try:
            result = audit_flag(conn, flag_id, seed_wallet_id)
            audited.append((flag_id, result["verdict"], time.perf_counter() - t0))
        except (pg_errors.QueryCanceled, pg_errors.OperationalError) as e:
            conn.rollback()
            skipped.append((flag_id, f"timeout after {timeout_seconds}s ({e.__class__.__name__})"))
        except Exception as e:  # noqa: BLE001 -- any other failure also must not kill the whole pass
            conn.rollback()
            skipped.append((flag_id, f"{e.__class__.__name__}: {e}"))
    return {"audited": audited, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--hub-indegree-cutoff", type=int, default=DEFAULT_HUB_INDEGREE_CUTOFF)
    parser.add_argument("--pagerank-limit", type=int, default=DEFAULT_PAGERANK_LIMIT)
    parser.add_argument("--sybil-limit", type=int, default=DEFAULT_SYBIL_LIMIT)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    plan = {}
    with conn.cursor() as cur:
        bfs_run_id = resolve_latest_run_id(cur, args.snapshot_id, "bfs_proximity")
        bfs_flag_ids, hub_wallet_ids = select_bfs_proximity_flag_ids(
            cur, args.snapshot_id, bfs_run_id, args.hub_indegree_cutoff
        )
        plan["bfs_proximity"] = bfs_flag_ids
        print(f"bfs_proximity: {len(bfs_flag_ids)} flags to audit, "
              f"{len(hub_wallet_ids)} known hub(s) excluded (indegree > {args.hub_indegree_cutoff}): "
              f"{sorted(hub_wallet_ids)}")

        for detector in ("impurity", "engaged_proximity"):
            run_id = resolve_latest_run_id(cur, args.snapshot_id, detector)
            plan[detector] = select_flag_ids(cur, run_id)
            print(f"{detector}: {len(plan[detector])} flags to audit (no limit)")

        pagerank_run_id = resolve_latest_run_id(cur, args.snapshot_id, "pagerank_threshold")
        plan["pagerank_threshold"] = select_flag_ids(cur, pagerank_run_id, limit=args.pagerank_limit)
        print(f"pagerank_threshold: {len(plan['pagerank_threshold'])} flags to audit "
              f"(limit {args.pagerank_limit})")

        sybil_run_id = resolve_latest_run_id(cur, args.snapshot_id, "sybil_cluster")
        plan["sybil_cluster"] = select_flag_ids(cur, sybil_run_id, limit=args.sybil_limit, random_order=True)
        print(f"sybil_cluster: {len(plan['sybil_cluster'])} flags to audit "
              f"(random sample of {args.sybil_limit})")

    t_start = time.time()
    results = {}
    for detector, flag_ids in plan.items():
        print(f"\n--- auditing {detector} ({len(flag_ids)} flags, timeout={args.timeout_seconds}s/flag) ---")
        t0 = time.time()
        results[detector] = run_audit_pass(conn, flag_ids, args.seed_wallet_id, args.timeout_seconds)
        elapsed = time.time() - t0
        n_audited = len(results[detector]["audited"])
        n_skipped = len(results[detector]["skipped"])
        rate = (n_audited / elapsed * 60) if elapsed > 0 and n_audited else 0.0
        print(f"{detector}: {n_audited} audited, {n_skipped} skipped, {elapsed:.1f}s ({rate:.1f} flags/min)")
        for flag_id, reason in results[detector]["skipped"]:
            print(f"  SKIPPED flag_id={flag_id}: {reason}")
    conn.close()

    total_elapsed = time.time() - t_start
    lines = ["# Full audit pass", ""]
    lines.append(f"snapshot_id={args.snapshot_id}, seed_wallet_id={args.seed_wallet_id}, "
                 f"timeout={args.timeout_seconds}s/flag, total runtime {total_elapsed / 60:.1f} min")
    lines.append("")
    lines.append("| Detector | Audited | Skipped | Elapsed (s) | Flags/min |")
    lines.append("|---|---|---|---|---|")
    for detector, result in results.items():
        n_audited = len(result["audited"])
        n_skipped = len(result["skipped"])
        elapsed = sum(t for _f, _v, t in result["audited"]) + n_skipped * args.timeout_seconds
        rate = (n_audited / elapsed * 60) if elapsed > 0 and n_audited else 0.0
        lines.append(f"| {detector} | {n_audited} | {n_skipped} | {elapsed:.1f} | {rate:.1f} |")
    report = "\n".join(lines)
    print("\n" + report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
