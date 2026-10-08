"""
Offline tests for graph/audit.py's pure verdict/cost logic, plus the
audit-level launderer/dusted scenarios from the corrected spec (built as
concrete transfer chains / tiny graphs, not requiring a database -- the
DB-I/O wrapper functions (audit_bfs_proximity etc.) are exercised against
the real database by hand, same split as the rest of this project).
"""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

import graph.audit as audit_module
from graph.audit import (
    cheapest_qualifying_bfs_transfer,
    cheapest_subset_reaching_threshold,
    verdict_from_score,
)
from graph.backend import GraphBackend
from graph.detectors import bfs_proximity_score, compute_engaged_outflow_ratios

SEED = 1


class TestVerdictFromScore(unittest.TestCase):
    def test_holds_above_threshold_is_frameable(self):
        self.assertEqual(verdict_from_score(70.0, 50.0), "frameable")

    def test_below_threshold_is_robust(self):
        self.assertEqual(verdict_from_score(30.0, 50.0), "robust")

    def test_exactly_at_threshold_is_frameable(self):
        self.assertEqual(verdict_from_score(50.0, 50.0), "frameable")


class TestCheapestQualifyingBfsTransfer(unittest.TestCase):
    def test_picks_the_cheaper_of_two_qualifying_senders(self):
        # both senders are 1 hop from the seed (distance=1), so either
        # would give W a qualifying distance of 2 -- pick the cheaper one
        candidates = [
            ("0xaaa", 1000.0, 10.0, 1),  # cost 1010
            ("0xbbb", 500.0, 5.0, 1),    # cost 505 -- cheaper
        ]
        result = cheapest_qualifying_bfs_transfer(candidates, threshold=50.0)
        self.assertEqual(result, ("0xbbb", 505.0))

    def test_ignores_non_qualifying_senders_even_if_cheaper(self):
        candidates = [
            ("0xaaa", 1.0, 1.0, 5),     # distance 5 -> doesn't qualify even though dirt cheap
            ("0xbbb", 500.0, 5.0, 1),   # distance 1 -> qualifies
        ]
        result = cheapest_qualifying_bfs_transfer(candidates, threshold=50.0)
        self.assertEqual(result, ("0xbbb", 505.0))

    def test_none_when_no_sender_qualifies(self):
        candidates = [("0xaaa", 1.0, 1.0, 10)]
        self.assertIsNone(cheapest_qualifying_bfs_transfer(candidates, threshold=50.0))


class TestCheapestSubsetReachingThreshold(unittest.TestCase):
    def test_exact_branch_finds_true_minimum_not_just_greedy_order(self):
        # Three candidates with costs 10, 10, 100 contributing 0.3, 0.3, 1.0
        # respectively toward a 0.5 threshold -- greedy-cheapest-first would
        # grab both 10-cost ones (total 0.6 >= 0.5, cost 20), which IS
        # actually optimal here, so construct a case where picking the
        # single big one alone is cheaper than two small ones combined.
        candidates = [("a", 10.0), ("b", 10.0), ("c", 15.0)]
        contributions = {"a": 0.3, "b": 0.3, "c": 0.5}

        def score_fn(tx_hashes):
            return sum(contributions[h] for h in tx_hashes)

        tx_hashes, cost, method = cheapest_subset_reaching_threshold(candidates, score_fn, threshold=0.5)
        self.assertEqual(method, "exact")
        self.assertEqual(sorted(tx_hashes), ["c"])  # single 15-cost beats the 20-cost pair
        self.assertEqual(cost, 15.0)

    def test_greedy_branch_used_above_the_exact_cutoff(self):
        candidates = [(f"tx{i}", float(i + 1)) for i in range(20)]  # 20 > EXACT_SUBSET_CUTOFF (15)
        contributions = {f"tx{i}": 1.0 for i in range(20)}

        def score_fn(tx_hashes):
            return sum(contributions[h] for h in tx_hashes)

        tx_hashes, cost, method = cheapest_subset_reaching_threshold(candidates, score_fn, threshold=3.0)
        self.assertEqual(method, "greedy_upper_bound")
        self.assertGreaterEqual(len(tx_hashes), 3)

    def test_prune_removes_members_not_actually_needed(self):
        # cheapest-first greedy (forced via a large candidate list) adds
        # three cheap candidates before reaching threshold, but only two
        # are actually needed -- the prune pass must drop the extra one.
        candidates = [(f"tx{i}", 1.0) for i in range(20)]
        contributions = {f"tx{i}": 2.0 for i in range(20)}  # any 2 of them already clear threshold=3.5

        def score_fn(tx_hashes):
            return sum(contributions[h] for h in tx_hashes)

        tx_hashes, cost, method = cheapest_subset_reaching_threshold(candidates, score_fn, threshold=3.5)
        self.assertEqual(len(tx_hashes), 2)
        self.assertEqual(cost, 2.0)

    def test_no_candidates_returns_none(self):
        tx_hashes, cost, method = cheapest_subset_reaching_threshold([], lambda h: 0.0, threshold=0.5)
        self.assertIsNone(tx_hashes)
        self.assertIsNone(cost)


class TestAuditLevelScenarios(unittest.TestCase):
    """The corrected spec's required audit verdicts, as concrete,
    hand-verifiable chains/graphs -- these exercise the SAME score-
    recomputation logic the real audit_* DB wrappers use (minus the DB),
    proving the frameable/robust mechanics themselves, not just the
    generic helpers above."""

    def test_launderer_is_robust_under_engaged_proximity_once_its_own_forwarding_is_removed(self):
        transfers_with_laundering = [
            (SEED, 2, 100.0, None, 100),
            (2, 9, 80.0, None, 105),  # wallet 2's OWN signed forwarding -- this is what gets removed
        ]
        signed_removed = [t for t in transfers_with_laundering if t[0] != 2]
        ratios = compute_engaged_outflow_ratios(signed_removed, SEED)
        new_score = ratios.get(2, {}).get(None, 0.0)
        self.assertEqual(verdict_from_score(new_score, threshold=0.5), "robust")

    def test_dusted_wallet_stays_frameable_under_bfs_proximity_with_signed_removed(self):
        """A purely passive wallet has no signed (outgoing) edges to
        remove in the first place -- bfs_proximity's score is provably
        unaffected by removing a wallet's OWN outgoing edges (distance
        INTO it only depends on edges pointing at its neighborhood), so
        the flag must still hold on the W-passive graph exactly as it did
        originally. Verified here with a real BFS re-run, not asserted
        from the reasoning alone."""
        edges = pd.DataFrame([
            {"src_wallet_id": SEED, "dst_wallet_id": 2, "edge_weight": 1.0},
        ])
        backend = GraphBackend(force_cpu=True)
        original_distance = backend.bfs(backend.load_edgelist(edges), SEED)
        original_score = bfs_proximity_score(float(original_distance[original_distance.wallet_id == 2].iloc[0].distance))

        # wallet 2 has no signed edges at all here, so "removing" them is a no-op
        masked = edges[edges.src_wallet_id != 2]
        new_distance = backend.bfs(backend.load_edgelist(masked), SEED)
        new_score = bfs_proximity_score(float(new_distance[new_distance.wallet_id == 2].iloc[0].distance))

        self.assertEqual(original_score, new_score)
        self.assertEqual(verdict_from_score(new_score, threshold=50.0), "frameable")

    def test_removing_a_wallets_own_outgoing_edge_never_changes_its_own_bfs_distance(self):
        """The general invariant bfs_proximity's exactness relies on:
        wallet 2 forwards to wallet 3 (its own signed activity) -- removing
        that edge can only ever affect wallet 3's distance, never wallet
        2's own distance from the seed."""
        edges = pd.DataFrame([
            {"src_wallet_id": SEED, "dst_wallet_id": 2, "edge_weight": 1.0},
            {"src_wallet_id": 2, "dst_wallet_id": 3, "edge_weight": 1.0},
        ])
        backend = GraphBackend(force_cpu=True)
        full = backend.bfs(backend.load_edgelist(edges), SEED)
        full_distance_2 = float(full[full.wallet_id == 2].iloc[0].distance)

        masked = edges[edges.src_wallet_id != 2]  # removes wallet 2's own signed edge to 3
        reduced = backend.bfs(backend.load_edgelist(masked), SEED)
        reduced_distance_2 = float(reduced[reduced.wallet_id == 2].iloc[0].distance)

        self.assertEqual(full_distance_2, reduced_distance_2)


class TestAuditFlagCollectsEvidenceForSybilCluster(unittest.TestCase):
    """sybil_cluster's yes_no method never calls fetch_unsolicited_
    candidates (no framing cost to search for), so unlike the other four
    detectors it never triggers evidence collection as a side effect --
    found live: a real audited sybil_cluster flag's dashboard drill-down
    showed "No evidence collected for this flag yet" forever. audit_flag
    must call collect_evidence_for_flag itself for this one detector.
    Wiring-only test (mocks every DB-I/O call) -- the real collection was
    verified by hand against the database (flag_id=16188, wallet 2339:
    505 evidence rows collected, now showing a real signed/unsolicited
    mix in the dashboard)."""

    def test_sybil_cluster_dispatch_calls_collect_evidence_for_flag(self):
        fake_flag = {
            "flag_id": 99, "run_id": 1, "wallet_id": 2339, "detector": "sybil_cluster",
            "score": 50.0, "threshold": None, "token_contract": None,
            "snapshot_id": 5, "parameters": {"louvain_run_id": 7},
        }
        with mock.patch.object(audit_module, "fetch_flag", return_value=fake_flag), \
             mock.patch.object(audit_module, "audit_sybil_cluster",
                                return_value={"verdict": "robust", "framing_cost_wei": None,
                                              "framing_gas": None, "minimal_tx_set": None,
                                              "method": "yes_no", "backend": "cpu-replay", "runtime_ms": 1}) as mock_audit, \
             mock.patch.object(audit_module, "collect_evidence_for_flag") as mock_collect, \
             mock.patch.object(audit_module, "persist_audit") as mock_persist:
            audit_module.audit_flag(conn=object(), flag_id=99, seed_wallet_id=1476)

        mock_audit.assert_called_once()
        mock_collect.assert_called_once_with(mock.ANY, 99, 5, 2339, 1476)
        mock_persist.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
