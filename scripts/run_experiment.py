"""
FrameCheck experiment runner: injects a framing scenario into the isolated
framecheck_sim database (see scripts/simulate_framing.py -- this script never
touches the main database for injection or analysis), re-exports a
snapshot and re-runs analyze/detect/evidence/audit THERE, then reports a
summary: per detector, how many of the injected target wallets got
flagged, what share of those came back frameable, and the framing-cost
range among them.

    python scripts/run_experiment.py --snapshot-id 4 --seed-wallet-id 1476
    python scripts/run_experiment.py --cleanup <scenario_id>
    python scripts/run_experiment.py --drop-db
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -- for ingestion/graph imports
sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ itself -- for simulate_framing
sys.stdout.reconfigure(line_buffering=True)

import psycopg2

from simulate_framing import (
    SIM_DB_NAME, ensure_sim_db_exists, drop_sim_db, cleanup_scenario,
    inject_framing_scenario, inject_launderer_scenario, cleanup_launderer_scenario,
    resolve_latest_bfs_run_id, _sim_dsn,
    DEFAULT_NUM_LAUNDERERS, DEFAULT_LAUNDER_ETH, DEFAULT_LAUNDER_FORWARD_FRACTION, LAUNDER_FORWARD_BLOCK_OFFSET,
)

SNAPSHOT_ID_RE = re.compile(r"snapshot_id=(\d+)")


def experiment_snapshot_name(scenario_id: int, launderer_scenario_id: int = None) -> str:
    base = f"experiment_{scenario_id}"
    return base if launderer_scenario_id is None else f"{base}_{launderer_scenario_id}"


def cleanup_experiment(conn, scenario_id: int, launderer_scenario_id: int = None) -> dict:
    """Removes everything run_experiment.py derived from this scenario --
    the experiment_<scenario_id>[_<launderer_scenario_id>] graph_snapshot
    and everything that references it (snapshot_edge, snapshot_edge_tx,
    analysis_run and its dependents: vertex_metric, cluster, cluster_member,
    alert, flag, flag_evidence, flag_audit) -- then the base injection(s)
    themselves via simulate_framing.cleanup_scenario() and, if a launderer
    scenario was also injected alongside the dust one,
    cleanup_launderer_scenario() too. Needed on top of that base cleanup
    because it only ever anticipated cleaning up the raw injection, not a
    full export+analyze+detect+evidence+audit chain run on top of it
    (that chain is this script's own addition)."""
    snapshot_name = experiment_snapshot_name(scenario_id, launderer_scenario_id)
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_id FROM graph_snapshot WHERE snapshot_name = %s", (snapshot_name,))
        snapshot_ids = [row[0] for row in cur.fetchall()]

        for snapshot_id in snapshot_ids:
            cur.execute("SELECT run_id FROM analysis_run WHERE snapshot_id = %s", (snapshot_id,))
            run_ids = [row[0] for row in cur.fetchall()]
            if run_ids:
                cur.execute("DELETE FROM flag_audit WHERE flag_id IN (SELECT flag_id FROM flag WHERE run_id = ANY(%s))", (run_ids,))
                cur.execute("DELETE FROM flag_evidence WHERE flag_id IN (SELECT flag_id FROM flag WHERE run_id = ANY(%s))", (run_ids,))
                cur.execute("DELETE FROM flag WHERE run_id = ANY(%s)", (run_ids,))
                cur.execute("DELETE FROM alert WHERE run_id = ANY(%s)", (run_ids,))
                cur.execute(
                    "DELETE FROM cluster_member WHERE cluster_id IN (SELECT cluster_id FROM cluster WHERE run_id = ANY(%s))",
                    (run_ids,),
                )
                cur.execute("DELETE FROM cluster WHERE run_id = ANY(%s)", (run_ids,))
                cur.execute("DELETE FROM vertex_metric WHERE run_id = ANY(%s)", (run_ids,))
                cur.execute("DELETE FROM analysis_run WHERE run_id = ANY(%s)", (run_ids,))
            cur.execute(
                "DELETE FROM snapshot_edge_tx WHERE snapshot_edge_id IN (SELECT edge_id FROM snapshot_edge WHERE snapshot_id = %s)",
                (snapshot_id,),
            )
            cur.execute("DELETE FROM snapshot_edge WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM graph_snapshot WHERE snapshot_id = %s", (snapshot_id,))
    conn.commit()

    base_result = cleanup_scenario(conn, scenario_id)
    result = {"snapshots_removed": len(snapshot_ids), **base_result}
    if launderer_scenario_id is not None:
        result["launderer"] = cleanup_launderer_scenario(conn, launderer_scenario_id)
    return result


def sim_env() -> dict:
    """Env for subprocess stages that must run against framecheck_sim, never
    the main database -- every other PG* var is inherited unchanged."""
    env = dict(os.environ)
    env["PGDATABASE"] = SIM_DB_NAME
    return env


def run(cmd, **kwargs):
    print(f"\n$ PGDATABASE={SIM_DB_NAME} {' '.join(cmd)}")
    result = subprocess.run(cmd, env=sim_env(), **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"[ABORT] stage failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, help="snapshot (in framecheck_sim) whose BFS run defines the targets")
    parser.add_argument("--seed-wallet-id", type=int)
    parser.add_argument("--num-targets", type=int, default=100)
    parser.add_argument("--dust-eth", type=float, default=0.001)
    parser.add_argument("--num-launderers", type=int, default=0,
                         help="also inject this many 'engaged launderer' wallets (receive from the seed, "
                              "forward most of it onward fast) alongside the passive dust targets; 0 (default) "
                              "skips this entirely, matching the original dust-only experiment")
    parser.add_argument("--launder-eth", type=float, default=DEFAULT_LAUNDER_ETH)
    parser.add_argument("--launder-forward-fraction", type=float, default=DEFAULT_LAUNDER_FORWARD_FRACTION)
    parser.add_argument("--cleanup", type=int, default=None, metavar="SCENARIO_ID")
    parser.add_argument("--cleanup-launderer", type=int, default=None, metavar="LAUNDERER_SCENARIO_ID",
                         help="pair with --cleanup when the run also injected launderers")
    parser.add_argument("--drop-db", action="store_true")
    args = parser.parse_args()

    if args.drop_db:
        drop_sim_db()
        print(f"dropped database '{SIM_DB_NAME}'")
        return

    created = ensure_sim_db_exists()
    if created:
        print(f"created simulation database '{SIM_DB_NAME}' (cloned from the main database)")

    sim_conn = psycopg2.connect(**_sim_dsn())
    try:
        if args.cleanup is not None:
            result = cleanup_experiment(sim_conn, args.cleanup, args.cleanup_launderer)
            print(f"cleaned up scenario {args.cleanup}"
                  f"{f' + launderer scenario {args.cleanup_launderer}' if args.cleanup_launderer else ''}: {result}")
            return

        if args.snapshot_id is None or args.seed_wallet_id is None:
            parser.error("--snapshot-id and --seed-wallet-id are required unless --cleanup or --drop-db is given")

        bfs_run_id = resolve_latest_bfs_run_id(sim_conn, args.snapshot_id)
        scenario_id = inject_framing_scenario(sim_conn, args.seed_wallet_id, bfs_run_id, args.num_targets, args.dust_eth)
        with sim_conn.cursor() as cur:
            cur.execute("SELECT target_wallet_ids FROM framing_scenario WHERE scenario_id = %s", (scenario_id,))
            target_wallet_ids = cur.fetchone()[0]
        print(f"scenario_id={scenario_id}: injected dust into {len(target_wallet_ids)} target wallet(s)")

        launderer_scenario_id = None
        launderer_wallet_ids = []
        if args.num_launderers > 0:
            launder_result = inject_launderer_scenario(
                sim_conn, args.seed_wallet_id, args.num_launderers, args.launder_eth, args.launder_forward_fraction,
            )
            launderer_scenario_id = launder_result["scenario_id"]
            launderer_wallet_ids = launder_result["launderer_wallet_ids"]
            print(f"scenario_id={launderer_scenario_id}: injected {len(launderer_wallet_ids)} launderer wallet(s) "
                  f"(each receives {args.launder_eth} ETH from the seed, forwards "
                  f"{args.launder_forward_fraction * 100:.0f}% onward within "
                  f"{LAUNDER_FORWARD_BLOCK_OFFSET} blocks)")
    finally:
        sim_conn.close()

    # Re-export + re-run analyze/detect/evidence/audit -- all against
    # framecheck_sim (sim_env()/PGDATABASE override), never the main database.
    snapshot_name = experiment_snapshot_name(scenario_id, launderer_scenario_id)
    export_result = run(
        [sys.executable, "-m", "graph.export_snapshot", "--name", snapshot_name,
         "--start-block", "0", "--end-block", "999999999"],
        capture_output=True, text=True,
    )
    print(export_result.stdout.strip())
    match = SNAPSHOT_ID_RE.search(export_result.stdout)
    if not match:
        raise SystemExit(f"Could not find snapshot_id in export_snapshot output: {export_result.stdout!r}")
    new_snapshot_id = int(match.group(1))

    run([sys.executable, "-m", "graph.run_analysis",
         "--snapshot-id", str(new_snapshot_id), "--seed-wallet-id", str(args.seed_wallet_id)])
    run([sys.executable, "-m", "graph.detectors",
         "--snapshot-id", str(new_snapshot_id), "--seed-wallet-id", str(args.seed_wallet_id), "--detector", "all"])

    # Evidence + audit, scoped to flags on the INJECTED wallets only (dust
    # victims + launderers, NOT their downstream recipients -- those are
    # just sinks, not the subject of this experiment) -- not the whole
    # snapshot. A real run can produce thousands of sybil_cluster flags
    # alone; this experiment only cares about the wallets it just
    # injected, so collecting/auditing everyone else's flags would be
    # pure waste. In-process (not a subprocess per flag) against the sim
    # database directly.
    from graph.evidence import collect_evidence_for_flag
    from graph.audit import audit_flag

    subject_wallet_ids = list(target_wallet_ids) + list(launderer_wallet_ids)
    sim_conn = psycopg2.connect(**_sim_dsn())
    try:
        with sim_conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.flag_id, f.wallet_id FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
                WHERE ar.snapshot_id = %s AND f.wallet_id = ANY(%s)
                """,
                (new_snapshot_id, subject_wallet_ids),
            )
            subject_flags = cur.fetchall()
        print(f"\n[evidence+audit] {len(subject_flags)} flag(s) on injected wallets...")
        for flag_id, wallet_id in subject_flags:
            collect_evidence_for_flag(sim_conn, flag_id, new_snapshot_id, wallet_id, args.seed_wallet_id)
            audit_flag(sim_conn, flag_id, args.seed_wallet_id)
    finally:
        sim_conn.close()

    # Summary, per detector: victims (dust) and launderers reported
    # side by side -- flagged / frameable / robust counts for each group.
    # When no launderers were injected, the launderer columns are all 0
    # and read the same as the original dust-only summary.
    sim_conn = psycopg2.connect(**_sim_dsn())
    try:
        with sim_conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.detector,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(victims)s)) AS victims_flagged,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(victims)s) AND fa.verdict = 'frameable') AS victims_frameable,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(victims)s) AND fa.verdict = 'robust') AS victims_robust,
                       MIN(fa.framing_cost_wei) FILTER (WHERE f.wallet_id = ANY(%(victims)s) AND fa.verdict = 'frameable') AS victims_cost_min,
                       MAX(fa.framing_cost_wei) FILTER (WHERE f.wallet_id = ANY(%(victims)s) AND fa.verdict = 'frameable') AS victims_cost_max,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(launderers)s)) AS launderers_flagged,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(launderers)s) AND fa.verdict = 'frameable') AS launderers_frameable,
                       COUNT(DISTINCT f.wallet_id) FILTER (WHERE f.wallet_id = ANY(%(launderers)s) AND fa.verdict = 'robust') AS launderers_robust
                FROM flag f
                JOIN analysis_run ar ON ar.run_id = f.run_id
                LEFT JOIN flag_audit fa ON fa.flag_id = f.flag_id
                WHERE ar.snapshot_id = %(snapshot_id)s AND f.wallet_id = ANY(%(subjects)s)
                GROUP BY f.detector ORDER BY f.detector
                """,
                {
                    "victims": target_wallet_ids, "launderers": launderer_wallet_ids,
                    "snapshot_id": new_snapshot_id, "subjects": subject_wallet_ids,
                },
            )
            rows = cur.fetchall()
    finally:
        sim_conn.close()

    print("\n" + "=" * 70)
    print(f"Experiment summary (dust scenario_id={scenario_id}, {len(target_wallet_ids)} victims"
          + (f"; launderer scenario_id={launderer_scenario_id}, {len(launderer_wallet_ids)} launderers" if launderer_scenario_id else "")
          + ")")
    print("=" * 70)
    if not rows:
        print("No injected wallet was flagged by any detector.")
    header = f"{'detector':<20} {'victims flagged/frameable/robust':<34} {'launderers flagged/frameable/robust':<36} victim cost range"
    print(header)
    for (detector, v_flagged, v_frameable, v_robust, v_cost_min, v_cost_max,
         l_flagged, l_frameable, l_robust) in rows:
        v_cell = f"{v_flagged}/{v_frameable}/{v_robust}"
        l_cell = f"{l_flagged}/{l_frameable}/{l_robust}"
        cost_range = f"{v_cost_min}-{v_cost_max} wei" if v_cost_min is not None else "n/a"
        print(f"{detector:<20} {v_cell:<34} {l_cell:<36} {cost_range}")

    cleanup_cmd = f"python scripts/run_experiment.py --cleanup {scenario_id}"
    if launderer_scenario_id:
        cleanup_cmd += f" --cleanup-launderer {launderer_scenario_id}"
    print(f"\ncleanup with: {cleanup_cmd}")


if __name__ == "__main__":
    main()
