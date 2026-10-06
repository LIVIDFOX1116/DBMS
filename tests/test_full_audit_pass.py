"""
Offline tests for scripts/full_audit_pass.py's pure/DB-shape helpers.
The real audit pass (run_audit_pass, with the statement_timeout/skip
mechanics) needs a real database with real flags and real slow/fast
wallets to mean anything -- verified by hand against snapshot 5; see the
commit message for the real throughput/skip numbers.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from full_audit_pass import select_bfs_proximity_flag_ids


class _FakeCursor:
    def __init__(self, flag_rows, indegree_rows):
        self._flag_rows = flag_rows
        self._indegree_rows = indegree_rows
        self._last = None

    def execute(self, query, params=None):
        self._last = "flag" if "FROM flag" in query else "indegree"

    def fetchall(self):
        return self._flag_rows if self._last == "flag" else self._indegree_rows


class TestSelectBfsProximityFlagIds(unittest.TestCase):
    def test_excludes_wallets_above_the_indegree_cutoff(self):
        cur = _FakeCursor(
            flag_rows=[(101, 2), (102, 3), (103, 4)],  # (flag_id, wallet_id)
            indegree_rows=[(2, 2005), (3, 40), (4, 10)],
        )
        kept, hubs = select_bfs_proximity_flag_ids(cur, snapshot_id=5, run_id=37, hub_indegree_cutoff=500)
        self.assertEqual(kept, [102, 103])
        self.assertEqual(hubs, [2])

    def test_wallet_missing_from_indegree_results_is_never_treated_as_a_hub(self):
        """A flagged wallet with ZERO inbound transactions in this
        snapshot simply never appears in the indegree query's result --
        must default to indegree 0, not be excluded."""
        cur = _FakeCursor(flag_rows=[(101, 2)], indegree_rows=[])
        kept, hubs = select_bfs_proximity_flag_ids(cur, snapshot_id=5, run_id=37, hub_indegree_cutoff=500)
        self.assertEqual(kept, [101])
        self.assertEqual(hubs, [])

    def test_no_flags_returns_empty_without_querying_indegree(self):
        cur = _FakeCursor(flag_rows=[], indegree_rows=[(2, 99999)])
        kept, hubs = select_bfs_proximity_flag_ids(cur, snapshot_id=5, run_id=37, hub_indegree_cutoff=500)
        self.assertEqual((kept, hubs), ([], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
