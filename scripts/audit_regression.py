"""
FrameCheck audit-result regression guard.

Phase 10's rule: a database change may make things faster, but it may not
change what FrameCheck concludes. This captures the flags, verdicts and
framing costs for a snapshot to a JSON file, and diffs two captures.

    python scripts/audit_regression.py capture --snapshot-id 2 --out before.json
    #   ... make the database change ...
    python scripts/audit_regression.py capture --snapshot-id 2 --out after.json
    python scripts/audit_regression.py diff before.json after.json

Exit code 1 if anything differs, so this can gate a change.

Pinned to the CPU backend on purpose. CPU and GPU PageRank disagree on this
data (see docs/benchmark_results.md: 71% of edges carry weight 0 and the two
libraries resolve dangling vertices differently), so a capture taken under
one backend and compared against the other would report a difference that
has nothing to do with the database.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2  # noqa: E402

from ingestion import config  # noqa: E402

FLAG_SQL = """
    SELECT f.flag_id, f.detector, f.wallet_id,
           f.score::text, f.threshold::text, f.token_contract
    FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
    WHERE ar.snapshot_id = %s
    ORDER BY f.flag_id
"""

AUDIT_SQL = """
    SELECT fa.flag_id, fa.verdict, fa.framing_cost_wei::text,
           fa.method, fa.minimal_tx_set
    FROM (
        SELECT DISTINCT ON (flag_id) *
        FROM flag_audit ORDER BY flag_id, created_at DESC, flag_audit_id DESC
    ) fa
    JOIN flag f ON f.flag_id = fa.flag_id
    JOIN analysis_run ar ON ar.run_id = f.run_id
    WHERE ar.snapshot_id = %s
    ORDER BY fa.flag_id
"""


def capture(snapshot_id: int) -> dict:
    conn = psycopg2.connect(**config.PG_DSN)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(FLAG_SQL, (snapshot_id,))
            flags = [list(map(_norm, row)) for row in cur.fetchall()]
            cur.execute(AUDIT_SQL, (snapshot_id,))
            audits = [list(map(_norm, row)) for row in cur.fetchall()]
    finally:
        conn.close()
    return {
        "snapshot_id": snapshot_id,
        "flag_count": len(flags),
        "audit_count": len(audits),
        # Hashes make an unchanged capture obvious at a glance; the rows
        # themselves are kept so a diff can say WHICH flag moved.
        "flags_sha256": _digest(flags),
        "audits_sha256": _digest(audits),
        "flags": flags,
        "audits": audits,
    }


def _norm(value):
    """Decimals and memoryviews do not round-trip through JSON identically;
    everything comparable is normalised to a string once, here, so a diff
    never reports a formatting artifact as a changed result."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return str(value)


def _digest(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def diff(before: dict, after: dict) -> int:
    problems = []
    if before["snapshot_id"] != after["snapshot_id"]:
        problems.append(f"different snapshots: {before['snapshot_id']} vs {after['snapshot_id']}")

    for section in ("flags", "audits"):
        b = {row[0]: row for row in before[section]}
        a = {row[0]: row for row in after[section]}
        missing = sorted(set(b) - set(a))
        added = sorted(set(a) - set(b))
        changed = [k for k in sorted(set(b) & set(a)) if b[k] != a[k]]
        if missing:
            problems.append(f"{section}: {len(missing)} disappeared, e.g. {missing[:5]}")
        if added:
            problems.append(f"{section}: {len(added)} appeared, e.g. {added[:5]}")
        for key in changed[:10]:
            problems.append(f"{section} {key} changed:\n    before {b[key]}\n    after  {a[key]}")
        if len(changed) > 10:
            problems.append(f"{section}: ...and {len(changed) - 10} more changed")

    print(f"before: {before['flag_count']} flags, {before['audit_count']} audits")
    print(f"after : {after['flag_count']} flags, {after['audit_count']} audits")
    print(f"flags  sha256 {'MATCH' if before['flags_sha256'] == after['flags_sha256'] else 'DIFFER'}")
    print(f"audits sha256 {'MATCH' if before['audits_sha256'] == after['audits_sha256'] else 'DIFFER'}")

    if problems:
        print("\nREGRESSION: the database change altered FrameCheck's conclusions.")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nIdentical. The change did not alter any flag, verdict or framing cost.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture")
    cap.add_argument("--snapshot-id", type=int, required=True)
    cap.add_argument("--out", type=str, required=True)

    dif = sub.add_parser("diff")
    dif.add_argument("before")
    dif.add_argument("after")

    args = parser.parse_args()
    if args.command == "capture":
        data = capture(args.snapshot_id)
        Path(args.out).write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"captured snapshot {args.snapshot_id}: "
              f"{data['flag_count']} flags, {data['audit_count']} audits -> {args.out}")
        print(f"  flags  sha256 {data['flags_sha256'][:16]}…")
        print(f"  audits sha256 {data['audits_sha256'][:16]}…")
        return 0
    return diff(json.loads(Path(args.before).read_text(encoding="utf-8")),
                json.loads(Path(args.after).read_text(encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(main())
