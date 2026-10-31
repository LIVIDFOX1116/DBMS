"""
FrameCheck data-quality checks.

Read-only. Every check answers one question that, if wrong, would quietly
corrupt an audit rather than crash it -- duplicate transactions inflating a
balance, a token transfer with no parent transaction (so no signer), a
snapshot edge pointing at a wallet that no longer exists, a framing cost
stored as a float.

    python scripts/check_data_quality.py
    python scripts/check_data_quality.py --snapshot-id 2

Exit code is 1 if any check fails, so this can gate a pipeline run.
"""
# RAW SQL (offline tooling): read-only data-quality report. Not part of the application's data access
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from ingestion import config  # noqa: E402

# Wei needs 27 digits at Ethereum's total supply (~1.2e26). A double carries
# ~15-16 significant digits, so anything above that silently rounds.
DOUBLE_SIGNIFICANT_DIGITS = 15


class Check:
    def __init__(self, name, sql, explain, params=None, warn_only=False):
        self.name = name
        self.sql = sql
        self.explain = explain
        self.params = params or ()
        self.warn_only = warn_only


def build_checks(snapshot_id):
    snapshot_filter = "" if snapshot_id is None else " AND se.snapshot_id = %s"
    snapshot_params = () if snapshot_id is None else (snapshot_id,)
    return [
        Check(
            "duplicate tx_hash",
            "SELECT tx_hash, count(*) FROM transaction GROUP BY tx_hash HAVING count(*) > 1 LIMIT 5",
            "The same transaction stored twice doubles its value in every replay.",
        ),
        Check(
            "token transfers with no parent transaction",
            """SELECT tt.tx_hash FROM token_transfer tt
               LEFT JOIN transaction t ON t.tx_hash = tt.tx_hash
               WHERE t.tx_hash IS NULL LIMIT 5""",
            "The signer comes from the transaction row; without it a transfer has no provable signer.",
        ),
        Check(
            "orphan snapshot_edge wallets",
            f"""SELECT se.edge_id FROM snapshot_edge se
                LEFT JOIN wallet w1 ON w1.wallet_id = se.src_wallet_id
                LEFT JOIN wallet w2 ON w2.wallet_id = se.dst_wallet_id
                WHERE (w1.wallet_id IS NULL OR w2.wallet_id IS NULL){snapshot_filter} LIMIT 5""",
            "An edge pointing at a missing wallet silently drops out of every join.",
            snapshot_params,
        ),
        Check(
            "snapshot_edge_tx rows with no parent edge",
            """SELECT st.tx_hash FROM snapshot_edge_tx st
               LEFT JOIN snapshot_edge se ON se.edge_id = st.snapshot_edge_id
               WHERE se.edge_id IS NULL LIMIT 5""",
            "Evidence collection walks edge -> tx; an orphan tx is unreachable.",
        ),
        Check(
            "flags pointing at a missing wallet",
            """SELECT f.flag_id FROM flag f
               LEFT JOIN wallet w ON w.wallet_id = f.wallet_id
               WHERE w.wallet_id IS NULL LIMIT 5""",
            "A flag with no wallet cannot be audited or displayed.",
        ),
        Check(
            "audits pointing at a missing flag",
            """SELECT fa.flag_audit_id FROM flag_audit fa
               LEFT JOIN flag f ON f.flag_id = fa.flag_id
               WHERE f.flag_id IS NULL LIMIT 5""",
            "An orphan verdict would be counted in /audit/summary with nothing behind it.",
        ),
        Check(
            "snapshot lineage: snapshots with no analysis run",
            """SELECT gs.snapshot_id FROM graph_snapshot gs
               LEFT JOIN analysis_run ar ON ar.snapshot_id = gs.snapshot_id
               WHERE ar.run_id IS NULL LIMIT 5""",
            "A snapshot nothing was ever run on -- usually an abandoned export.",
            warn_only=True,
        ),
        Check(
            "snapshot lineage: runs on a missing snapshot",
            """SELECT ar.run_id FROM analysis_run ar
               LEFT JOIN graph_snapshot gs ON gs.snapshot_id = ar.snapshot_id
               WHERE gs.snapshot_id IS NULL LIMIT 5""",
            "A run whose snapshot is gone cannot be reproduced or re-audited.",
        ),
        Check(
            "negative wei values",
            """SELECT tx_hash FROM transaction WHERE value_wei < 0 LIMIT 5""",
            "Wei is unsigned on-chain; a negative value means a parse or sign error.",
        ),
        Check(
            "framing costs that are negative",
            "SELECT flag_audit_id FROM flag_audit WHERE framing_cost_wei < 0 LIMIT 5",
            "A negative framing cost would make an attack look free.",
        ),
        Check(
            "verdict set to frameable with no framing cost",
            """SELECT flag_audit_id FROM flag_audit
               WHERE verdict = 'frameable' AND framing_cost_wei IS NULL
                 AND method <> 'yes_no' LIMIT 5""",
            "Frameable without a cost, outside the yes_no method, means the cost search was lost.",
            warn_only=True,
        ),
        Check(
            "self-loop edges",
            f"""SELECT se.edge_id FROM snapshot_edge se
                WHERE se.src_wallet_id = se.dst_wallet_id{snapshot_filter} LIMIT 5""",
            "Real data, not corruption: these are transactions where from == to (contract "
            "self-calls, value 0). Reported because they still sit in the PageRank/Louvain "
            "input, where a wallet pointing at itself is not a transfer between parties.",
            snapshot_params,
            warn_only=True,
        ),
        Check(
            "edge weights beyond double precision",
            f"""SELECT se.edge_id FROM snapshot_edge se
                WHERE se.edge_weight >= 1e{DOUBLE_SIGNIFICANT_DIGITS}{snapshot_filter} LIMIT 5""",
            f"snapshot_edge.edge_weight is DOUBLE PRECISION, which carries about "
            f"{DOUBLE_SIGNIFICANT_DIGITS} significant digits. Wei needs up to 27, so these "
            f"values are rounded. They feed PageRank/Louvain weighting, NOT framing cost "
            f"(which reads transaction.value_wei, a NUMERIC).",
            snapshot_params,
            warn_only=True,
        ),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-id", type=int, default=None,
                        help="restrict snapshot-scoped checks to one snapshot")
    args = parser.parse_args()

    conn = psycopg2.connect(**config.PG_DSN)
    conn.set_session(readonly=True)
    failures, warnings = 0, 0
    try:
        for check in build_checks(args.snapshot_id):
            with conn.cursor() as cur:
                cur.execute(check.sql, check.params)
                rows = cur.fetchall()
            if not rows:
                print(f"  ok    {check.name}")
                continue
            label = "WARN" if check.warn_only else "FAIL"
            if check.warn_only:
                warnings += 1
            else:
                failures += 1
            print(f"  {label}  {check.name}: {len(rows)} example(s) {rows[:3]}")
            print(f"        {check.explain}")
    finally:
        conn.close()

    print(f"\n{failures} failure(s), {warnings} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
