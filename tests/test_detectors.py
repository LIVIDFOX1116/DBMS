"""
Offline tests for graph/detectors.py's pure detection logic (no DB needed)
plus persist_flags's bulk-insert shape. The run_* DB-I/O wrappers are thin
glue over these pure functions and are exercised against the real database
by hand (see graph/detectors.py's module docstring) -- same split
run_analysis.py's compute_internal_edges/persist_clusters already uses.
"""
import ast
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from graph.detectors import (
    bfs_proximity_score,
    select_bfs_proximity_flags,
    select_sybil_cluster_flags,
    select_pagerank_threshold_flags,
    compute_impurity_ratios,
    select_impurity_flags,
    compute_engaged_outflow_ratios,
    select_engaged_proximity_flags,
    persist_flags,
)

SEED = 1


class TestBfsProximityScore(unittest.TestCase):
    def test_matches_score_wallets_formula_exactly(self):
        self.assertEqual(bfs_proximity_score(0), 90.0)
        self.assertEqual(bfs_proximity_score(1), 70.0)
        self.assertEqual(bfs_proximity_score(2), 50.0)
        self.assertEqual(bfs_proximity_score(3), 30.0)

    def test_floors_at_zero_rather_than_going_negative(self):
        self.assertEqual(bfs_proximity_score(10), 0.0)


class TestSelectBfsProximityFlags(unittest.TestCase):
    def test_flags_within_two_hops_excludes_seed_and_farther_wallets(self):
        distances = {SEED: 0, 2: 1, 3: 2, 4: 3}
        flags = select_bfs_proximity_flags(distances, SEED, threshold=50.0)
        self.assertEqual(dict(flags), {2: 70.0, 3: 50.0})

    def test_threshold_is_configurable(self):
        distances = {SEED: 0, 2: 1, 3: 2}
        flags = select_bfs_proximity_flags(distances, SEED, threshold=80.0)
        self.assertEqual(flags, [])


class TestSelectSybilClusterFlags(unittest.TestCase):
    def test_passes_through_classify_clusters_output_unchanged_minus_seed(self):
        members = [(SEED, 99.0), (2, 80.0), (3, 60.0)]
        flags = select_sybil_cluster_flags(members, SEED)
        self.assertEqual(flags, [(2, 80.0), (3, 60.0)])

    def test_does_not_invent_scores_for_wallets_not_in_input(self):
        self.assertEqual(select_sybil_cluster_flags([], SEED), [])


class TestSelectPagerankThresholdFlags(unittest.TestCase):
    def test_top_one_percent_excludes_seed(self):
        scores = [(SEED, 1000.0)] + [(i, float(i)) for i in range(1, 11)]
        flags, threshold = select_pagerank_threshold_flags(scores, SEED, percentile=0.01)
        self.assertEqual(flags, [(10, 10.0)])
        self.assertEqual(threshold, 10.0)

    def test_larger_percentile_includes_more_wallets(self):
        scores = [(SEED, 1000.0)] + [(i, float(i)) for i in range(1, 11)]
        flags, threshold = select_pagerank_threshold_flags(scores, SEED, percentile=0.2)
        self.assertEqual(flags, [(10, 10.0), (9, 9.0)])
        self.assertEqual(threshold, 9.0)

    def test_empty_after_excluding_seed_returns_empty_and_none(self):
        flags, threshold = select_pagerank_threshold_flags([(SEED, 5.0)], SEED, percentile=0.5)
        self.assertEqual(flags, [])
        self.assertIsNone(threshold)


class TestComputeImpurityRatios(unittest.TestCase):
    """Hand-verifiable transfer chain: seed -> A (100, fully tainted),
    A -> B (50, half of A's balance -- so fully tainted, since A is 100%
    tainted at that point), and an unrelated wallet C -> B (50, untainted
    -- C never received anything in this subgraph, so its tracked balance
    is 0 before it sends; the partial-balance floor means C contributes
    no taint and ends with balance 0, dropping out of the result)."""

    def test_known_chain_produces_hand_verified_ratios(self):
        transfers = [
            (SEED, 2, 100.0),  # A = wallet 2
            (2, 3, 50.0),      # B = wallet 3
            (4, 3, 50.0),      # C = wallet 4, untainted source outside the subgraph
        ]
        ratios = compute_impurity_ratios(transfers, SEED)
        self.assertAlmostEqual(ratios[2], 1.0)   # A: fully tainted
        self.assertAlmostEqual(ratios[3], 0.5)   # B: half its balance traces to the seed
        self.assertNotIn(4, ratios)              # C: balance floored to 0, excluded
        self.assertNotIn(SEED, ratios)           # seed itself never included

    def test_wallet_with_zero_balance_is_excluded_not_divided_by_zero(self):
        transfers = [(SEED, 2, 100.0), (2, 3, 100.0)]  # A forwards everything, ends at balance 0
        ratios = compute_impurity_ratios(transfers, SEED)
        self.assertNotIn(2, ratios)
        self.assertAlmostEqual(ratios[3], 1.0)


class TestEngagedProximity(unittest.TestCase):
    """The three narrative scenarios from the corrected spec, built as
    concrete numeric transfer chains with hand-worked expected ratios --
    this is the DETECTOR's own behavior (does it flag in the first
    place), not the audit's frameable/robust verdict on top of it."""

    BFS_DISTANCES = {SEED: 0, 2: 1}  # wallet 2 is always 1 hop from the seed in these scenarios

    def test_launderer_forwards_most_of_the_taint_quickly_and_is_flagged(self):
        transfers = [
            (SEED, 2, 100.0, 100),  # taint arrives at wallet 2, block 100
            (2, 9, 80.0, 105),      # wallet 2 forwards 80% of it onward 5 blocks later
        ]
        ratios = compute_engaged_outflow_ratios(transfers, SEED)
        self.assertAlmostEqual(ratios[2], 0.8)
        flags = select_engaged_proximity_flags(self.BFS_DISTANCES, ratios, SEED)
        self.assertEqual([w for w, score, ratio in flags], [2])

    def test_dusted_wallet_that_does_nothing_is_not_flagged(self):
        transfers = [(SEED, 2, 1.0, 100)]  # dust arrives; wallet 2 never sends anything
        ratios = compute_engaged_outflow_ratios(transfers, SEED)
        self.assertEqual(ratios.get(2, 0.0), 0.0)
        flags = select_engaged_proximity_flags(self.BFS_DISTANCES, ratios, SEED)
        self.assertEqual(flags, [])

    def test_busy_wallet_that_sends_mostly_unrelated_funds_is_not_flagged(self):
        transfers = [
            (SEED, 2, 1.0, 100),    # dust arrives
            (5, 2, 100.0, 102),     # a large UNRELATED, untainted deposit arrives from elsewhere
            (2, 9, 10.0, 105),      # wallet 2 sends only a small fraction of its now-mostly-clean balance
        ]
        ratios = compute_engaged_outflow_ratios(transfers, SEED)
        # taint_out = 10 * (1/101) ~= 0.099 -- well under half of the 1.0 wei it ever received tainted
        self.assertLess(ratios[2], 0.5)
        flags = select_engaged_proximity_flags(self.BFS_DISTANCES, ratios, SEED)
        self.assertEqual(flags, [])

    def test_forwarding_outside_the_window_does_not_count(self):
        transfers = [
            (SEED, 2, 100.0, 100),
            (2, 9, 80.0, 100 + 7200 + 1),  # one block past the default window
        ]
        ratios = compute_engaged_outflow_ratios(transfers, SEED, window_blocks=7200)
        self.assertEqual(ratios.get(2, 0.0), 0.0)

    def test_forwarding_right_at_the_window_edge_counts(self):
        transfers = [
            (SEED, 2, 100.0, 100),
            (2, 9, 80.0, 100 + 7200),  # exactly at the edge, inclusive
        ]
        ratios = compute_engaged_outflow_ratios(transfers, SEED, window_blocks=7200)
        self.assertAlmostEqual(ratios[2], 0.8)


class TestSelectImpurityFlags(unittest.TestCase):
    def test_filters_by_threshold(self):
        ratios = {2: 1.0, 3: 0.5, 4: 0.05}
        self.assertEqual(sorted(select_impurity_flags(ratios, 0.10)), [(2, 1.0), (3, 0.5)])
        self.assertEqual(select_impurity_flags(ratios, 0.6), [(2, 1.0)])


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def mogrify(self, template, args):
        rendered = template.decode()
        for value in args:
            rendered = rendered.replace("%s", repr(value), 1)
        return rendered.encode()

    def execute(self, query, params=None):
        if isinstance(query, bytes):
            query = query.decode()
        q = " ".join(query.split())
        match = re.search(r"VALUES\s+(.+?)(?:\s+ON CONFLICT|\s+RETURNING|$)", q, re.IGNORECASE | re.DOTALL)
        self._sink.extend(ast.literal_eval("[" + match.group(1) + "]"))

    @property
    def connection(self):
        return type("_C", (), {"encoding": "UTF8"})()


class _FakeConn:
    def __init__(self):
        self.rows = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self.rows)

    def commit(self):
        self.committed += 1


class TestPersistFlags(unittest.TestCase):
    def test_bulk_inserts_all_rows_in_one_call(self):
        conn = _FakeConn()
        rows = [(2, "bfs_proximity", 70.0, 50.0), (3, "impurity", 0.5, 0.10)]
        count = persist_flags(conn, run_id=99, rows=rows)
        self.assertEqual(count, 2)
        self.assertEqual(conn.rows, [(99, 2, "bfs_proximity", 70.0, 50.0), (99, 3, "impurity", 0.5, 0.10)])
        self.assertEqual(conn.committed, 1)

    def test_empty_rows_still_commits_and_returns_zero(self):
        conn = _FakeConn()
        count = persist_flags(conn, run_id=99, rows=[])
        self.assertEqual(count, 0)
        self.assertEqual(conn.committed, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
