"""
FrameCheck Phase 4: collects the transaction evidence a flag rests on.

For a flagged wallet W, runs the recursive CTE in db/queries/evidence.sql
(see that file's header for the exact walk semantics and role assignment)
and persists one flag_evidence row per discovered transaction.

    python -m graph.evidence --snapshot-id 3 --seed-wallet-id 1476 --run-id 20
    python -m graph.evidence --snapshot-id 3 --seed-wallet-id 1476   # every flag in the snapshot
"""
import argparse
import pathlib

import psycopg2
from psycopg2.extras import execute_values

from ingestion import config

QUERY_PATH = pathlib.Path(__file__).parent.parent / "db" / "queries" / "evidence.sql"

DEFAULT_MAX_HOPS = 10  # cycle/runaway guard for the recursive back-toward-the-seed walk


# RAW SQL (allowed): recursive CTE (db/queries/evidence.sql)
def fetch_evidence_chain(conn, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                          max_hops: int = DEFAULT_MAX_HOPS) -> list:
    """Returns [(tx_hash, hop, block_number, role), ...], block-ordered."""
    sql = QUERY_PATH.read_text()
    with conn.cursor() as cur:
        cur.execute(sql, {
            "snapshot_id": snapshot_id, "wallet_id": wallet_id,
            "seed_wallet_id": seed_wallet_id, "max_hops": max_hops,
        })
        return cur.fetchall()


# RAW SQL (allowed): performance-critical bulk write (execute_values)
def persist_evidence(conn, flag_id: int, chain: list, commit: bool = True) -> int:
    """chain: [(tx_hash, hop, block_number, role), ...]. ON CONFLICT DO
    NOTHING on the (flag_id, tx_hash) primary key makes re-collecting
    evidence for an already-processed flag a safe no-op rather than an
    error."""
    if not chain:
        if commit:
            conn.commit()
        return 0
    rows = [(flag_id, tx_hash, hop, block_number, role) for tx_hash, hop, block_number, role in chain]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO flag_evidence (flag_id, tx_hash, hop, block_number, role) VALUES %s ON CONFLICT DO NOTHING",
            rows,
        )
    if commit:
        conn.commit()
    return len(rows)


def collect_evidence_for_flag(conn, flag_id: int, snapshot_id: int, wallet_id: int, seed_wallet_id: int,
                               max_hops: int = DEFAULT_MAX_HOPS) -> int:
    chain = fetch_evidence_chain(conn, snapshot_id, wallet_id, seed_wallet_id, max_hops)
    return persist_evidence(conn, flag_id, chain)


# RAW SQL (not yet converted): batch-pipeline target selection on the caller's connection
def collect_evidence_for_run(conn, snapshot_id: int, seed_wallet_id: int, run_id: int = None,
                              max_hops: int = DEFAULT_MAX_HOPS) -> int:
    """Collects evidence for every flag row under `run_id` (or, if omitted,
    every flag tied to any analysis_run on this snapshot)."""
    with conn.cursor() as cur:
        if run_id is not None:
            cur.execute("SELECT flag_id, wallet_id FROM flag WHERE run_id = %s", (run_id,))
        else:
            cur.execute(
                """SELECT f.flag_id, f.wallet_id FROM flag f
                   JOIN analysis_run ar ON ar.run_id = f.run_id
                   WHERE ar.snapshot_id = %s""",
                (snapshot_id,),
            )
        flags = cur.fetchall()

    total = 0
    for flag_id, wallet_id in flags:
        total += collect_evidence_for_flag(conn, flag_id, snapshot_id, wallet_id, seed_wallet_id, max_hops)
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--seed-wallet-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, default=None,
                         help="limit to flags from this analysis_run; default: every flag tied to this snapshot")
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS)
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    try:
        total = collect_evidence_for_run(conn, args.snapshot_id, args.seed_wallet_id, args.run_id, args.max_hops)
        print(f"collected {total} evidence rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
