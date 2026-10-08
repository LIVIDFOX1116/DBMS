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



class TestExactSearchBudget(unittest.TestCase):
    """The exact framing-cost search stops at the first subset size that
    reaches the threshold, so a one-transfer framing stays cheap. A
    threshold NO subset reaches is the expensive case: it enumerates all
    2**n subsets, and one probe is a full history replay (or a full
    PageRank run). On a 254k-transfer snapshot that was hours per flag,
    so the probe count is now budgeted."""

    def test_probe_budget_is_the_affordable_number_of_score_calls(self):
        f = audit_module.max_probes_for_budget
        self.assertEqual(f(1_000, 50_000_000), 50_000)
        self.assertEqual(f(254_000, 50_000_000), 196)
        # One probe costing more than the whole budget still buys one probe.
        self.assertEqual(f(60_000_000, 50_000_000), 1)
        # Unknown/empty cost must not silently meter the search.
        self.assertIsNone(f(0, 50_000_000))

    def test_cheap_solution_stays_exact_even_on_a_tight_budget(self):
        # A single transfer reaches the threshold, so the search never needs
        # the sizes it couldn't afford: the verdict is still exact.
        candidates = [(f"h{i}", float(i + 1)) for i in range(15)]
        probes = []

        def score_fn(tx_hashes):
            probes.append(tuple(tx_hashes))
            return 1.0 if "h7" in tx_hashes else 0.0

        tx_hashes, cost, method = audit_module.cheapest_subset_reaching_threshold(
            candidates, score_fn, threshold=1.0, max_probes=20)
        self.assertEqual(method, "exact")
        self.assertEqual(tx_hashes, ["h7"])
        self.assertEqual(cost, 8.0)
        self.assertEqual(len(probes), len(candidates))  # one size-1 level only

    def test_unreachable_threshold_bails_out_to_greedy_instead_of_enumerating(self):
        candidates = [(f"h{i}", float(i + 1)) for i in range(15)]
        probes = []

        def score_fn(tx_hashes):
            probes.append(tuple(tx_hashes))
            return 0.0  # nothing ever reaches the threshold

        tx_hashes, cost, method = audit_module.cheapest_subset_reaching_threshold(
            candidates, score_fn, threshold=1.0, max_probes=20)
        self.assertIsNone(tx_hashes)
        self.assertEqual(method, "greedy_upper_bound")
        # Size 1 (15 probes) is affordable, size 2 (105 more) is not, then
        # greedy adds one candidate at a time. Unbudgeted this was 32,767.
        self.assertEqual(len(probes), 2 * len(candidates))

    def test_unreachable_threshold_within_budget_still_reports_exact(self):
        # Small enough to enumerate fully: "no subset works" is then a real
        # exact answer, not a budget bail-out.
        candidates = [("h1", 1.0), ("h2", 2.0)]
        tx_hashes, cost, method = audit_module.cheapest_subset_reaching_threshold(
            candidates, lambda _h: 0.0, threshold=1.0, max_probes=100)
        self.assertIsNone(tx_hashes)
        self.assertEqual(method, "exact")

    def test_haircut_audit_meters_the_search_by_history_size(self):
        seed, w, x, y = 1, 10, 20, 30
        all_transfers = [
            (seed, x, 100, None, 1, "h1"),
            (x, w, 100, None, 3, "h3"),
            (y, w, 100, None, 4, "h4"),
        ]
        candidates = [("h3", x, 100, 21000, 1, 3), ("h4", y, 100, 21000, 1, 4)]
        captured = {}

        def fake_search(cands, score_fn, threshold, exact_cutoff=None, max_probes=None):
            captured["max_probes"] = max_probes
            return None, None, "greedy_upper_bound"

        conn = mock.MagicMock()
        with mock.patch.object(audit_module, "_fetch_native_and_token_transfers_for_audit",
                               return_value=all_transfers), \
             mock.patch.object(audit_module, "fetch_unsolicited_candidates", return_value=candidates), \
             mock.patch.object(audit_module, "cheapest_subset_reaching_threshold", side_effect=fake_search), \
             mock.patch.object(audit_module, "EXACT_REPLAY_ROW_BUDGET", 9):
            audit_module.audit_impurity(conn, 1, 1, w, seed, threshold=0.5)
        self.assertEqual(captured["max_probes"], 3)  # 9 rows of budget / 3 rows per replay

    def test_pagerank_audit_meters_the_search_by_edge_count(self):
        w, s1, s2 = 10, 2, 3
        edges = pd.DataFrame({
            "src_wallet_id": [s1, s2, w],
            "dst_wallet_id": [w, w, 4],
            "edge_weight": [1.0, 1.0, 1.0],
        })
        candidates = [("tx1", s1, 5, 21000, 1, 1), ("tx2", s2, 5, 21000, 1, 2)]
        captured = {}

        def fake_search(cands, score_fn, threshold, exact_cutoff=None, max_probes=None):
            captured["max_probes"] = max_probes
            return None, None, "greedy_upper_bound"

        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("ignored.parquet",)
        with mock.patch.object(audit_module.pd, "read_parquet", return_value=edges), \
             mock.patch.object(audit_module, "fetch_unsolicited_candidates", return_value=candidates), \
             mock.patch.object(audit_module, "cheapest_subset_reaching_threshold", side_effect=fake_search), \
             mock.patch.object(audit_module, "EXACT_PAGERANK_EDGE_BUDGET", 6), \
             mock.patch.object(GraphBackend, "pagerank_excluding_signed_edges",
                               return_value=pd.DataFrame({"wallet_id": [w], "score": [1.0]})):
            audit_module.audit_pagerank_threshold(conn, 1, 1, w, 1, threshold_value=0.5)
        self.assertEqual(captured["max_probes"], 2)  # 6 edges of budget / 3 edges per run


class TestAuditCliFlagSelection(unittest.TestCase):
    def test_limit_takes_a_reproducible_sample(self):
        # The big detectors (60k sybil flags) can only be audited as a
        # bounded sample, and those numbers get quoted in docs -- so the
        # sample must not depend on whatever order Postgres felt like.
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        argv = ["graph.audit", "--snapshot-id", "2", "--seed-wallet-id", "2",
                "--detector", "sybil_cluster", "--limit", "25"]
        with mock.patch.object(audit_module.psycopg2, "connect", return_value=conn), \
             mock.patch.object(sys, "argv", argv):
            audit_module.main()
        query = cur.execute.call_args.args[0]
        self.assertIn("ORDER BY f.flag_id", query)
        self.assertLess(query.index("ORDER BY f.flag_id"), query.index("LIMIT"))


class TestPhase3SpecFixes(unittest.TestCase):
    """Fixes from the Phase 3 review of the audit engine against
    docs/FRAMECHECK.md. Each test pins one discrepancy."""

    def test_impurity_framing_cost_replays_added_back_transfer_in_its_original_position(self):
        # S -> X (100 tainted), X -> W (100, unsolicited, block 3), then X receives
        # 300 clean from Z at block 5. Replayed in order, X forwards 100% tainted to W
        # (ratio 1.0); replayed at the END, X's balance is 400 and only 25% is tainted.
        seed, w, x, z = 1, 10, 20, 30
        all_transfers = [
            (seed, x, 100, None, 1, "h1"),
            (x, w, 100, None, 3, "h3"),
            (z, x, 300, None, 5, "h4"),
        ]
        candidates = [("h3", x, 100, 21000, 1, 3)]
        conn = mock.MagicMock()
        with mock.patch.object(audit_module, "_fetch_native_and_token_transfers_for_audit",
                               return_value=all_transfers), \
             mock.patch.object(audit_module, "fetch_unsolicited_candidates", return_value=candidates):
            result = audit_module.audit_impurity(conn, 1, 1, w, seed, threshold=0.5)
        self.assertEqual(result["verdict"], "frameable")
        self.assertEqual(result["minimal_tx_set"], ["h3"])
        self.assertEqual(result["framing_cost_wei"], 100 + 21000)

    def test_bfs_framing_cost_ignores_senders_only_reachable_through_the_flagged_wallet(self):
        # seed(1) -> A(2) -> W(10); W -> S(3) (W's own signed edge); A -> W and S -> W are
        # both unsolicited candidates. S is only reachable via W, so once W's signed edges
        # are removed it is not a valid cheap path; A must be picked even though S is cheaper.
        seed, a, w, s = 1, 2, 10, 3
        edges = pd.DataFrame({
            "src_wallet_id": [seed, a, w, s, a],
            "dst_wallet_id": [a, w, s, w, w],
            "edge_weight": [1.0] * 5,
        })
        candidates = [
            ("tx_s", s, 1, 0, 0, 3),
            ("tx_a", a, 100, 0, 0, 2),
        ]
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("ignored.parquet",)
        with mock.patch.object(audit_module.pd, "read_parquet", return_value=edges), \
             mock.patch.object(audit_module, "fetch_unsolicited_candidates", return_value=candidates):
            result = audit_module.audit_bfs_proximity(conn, 1, 1, w, seed, threshold=10.0, bfs_run_id=None)
        self.assertEqual(result["verdict"], "frameable")
        self.assertEqual(result["minimal_tx_set"], ["tx_a"])

    def test_replay_fetches_exclude_reverted_transactions(self):
        from graph import detectors

        for fetch in (
            lambda cur: audit_module._fetch_native_and_token_transfers_for_audit(cur, 1),
            lambda cur: detectors._fetch_native_and_token_transfers(cur, 1, with_block=True),
        ):
            cur = mock.MagicMock()
            cur.fetchall.return_value = []
            fetch(cur)
            queries = [c.args[0] for c in cur.execute.call_args_list]
            self.assertEqual(len(queries), 2)
            for q in queries:
                self.assertIn(detectors.NOT_REVERTED_SQL, q)

    def test_reverted_filter_only_applies_from_byzantium_on(self):
        from graph.detectors import BYZANTIUM_BLOCK, NOT_REVERTED_SQL

        self.assertEqual(BYZANTIUM_BLOCK, 4_370_000)
        self.assertIn("t.status IS FALSE", NOT_REVERTED_SQL)
        self.assertIn(f"t.block_number >= {BYZANTIUM_BLOCK}", NOT_REVERTED_SQL)

    def test_pagerank_cost_search_skips_senders_with_no_edge_into_the_wallet(self):
        # token_sender only appears in a token transfer, so there is no src->W edge to restore
        w, native_sender, token_sender = 10, 2, 3
        edges = pd.DataFrame({
            "src_wallet_id": [native_sender, 4, w],
            "dst_wallet_id": [w, native_sender, 4],
            "edge_weight": [1.0, 1.0, 1.0],
        })
        candidates = [
            ("tx_token", token_sender, 0, 21000, 1, 1),
            ("tx_native", native_sender, 5, 21000, 1, 2),
        ]
        seen = []

        def fake_search(cands, score_fn, threshold, **kw):
            seen.extend(cands)
            return None, None, "greedy_upper_bound"

        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("ignored.parquet",)
        with mock.patch.object(audit_module.pd, "read_parquet", return_value=edges), \
             mock.patch.object(audit_module, "fetch_unsolicited_candidates", return_value=candidates), \
             mock.patch.object(audit_module, "cheapest_subset_reaching_threshold", side_effect=fake_search), \
             mock.patch.object(GraphBackend, "pagerank_excluding_signed_edges",
                               return_value=pd.DataFrame({"wallet_id": [w], "score": [1.0]})):
            audit_module.audit_pagerank_threshold(conn, 1, 1, w, 1, threshold_value=0.5)
        self.assertEqual([c[0] for c in seen], [native_sender])


if __name__ == "__main__":
    unittest.main(verbosity=2)
