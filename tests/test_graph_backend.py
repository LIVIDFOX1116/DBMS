"""
Exercises GraphBackend on a small synthetic edge list, against both the CPU
(pandas + NetworkX) and GPU (cudf + cugraph) paths. The CPU class always
runs -- it's the path that actually runs in the sandbox and on the Zenbook.
The GPU class runs the exact same test bodies unmodified and is skipped
automatically wherever RAPIDS isn't importable (i.e. everywhere except the
GTX 1650 / RTX 4070 laptops with the framecheck-gpu conda env active).

GraphBackend's GPU path is opt-in (FRAMECHECK_USE_GPU_GRAPH=1) as of the
provenance work -- gpu_available() now returns False without it regardless
of whether RAPIDS is importable. This test file explicitly opts in for
itself (setdefault, not a hard override, so a caller who deliberately unset
it to test the fallback-when-not-opted-in behavior isn't fought with) so
TestGraphBackendGPU's skip condition below still reflects RAPIDS
importability the way it always has, not the new gate.
"""
import os
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
os.environ.setdefault("FRAMECHECK_USE_GPU_GRAPH", "1")

import pandas as pd

from graph import backend
from graph.backend import GraphBackend, gpu_available


def synthetic_edges():
    # A small hub-and-spoke graph: wallet 1 is a clear PageRank hub (in-degree
    # 3), wallets 4-5 form an obviously separate community from 1-3.
    return pd.DataFrame([
        {"src_wallet_id": 2, "dst_wallet_id": 1, "edge_weight": 5.0},
        {"src_wallet_id": 3, "dst_wallet_id": 1, "edge_weight": 3.0},
        {"src_wallet_id": 1, "dst_wallet_id": 2, "edge_weight": 1.0},
        {"src_wallet_id": 4, "dst_wallet_id": 5, "edge_weight": 2.0},
        {"src_wallet_id": 5, "dst_wallet_id": 4, "edge_weight": 2.0},
    ])


class _GraphBackendTests:
    """Shared test bodies -- mixed into the CPU and GPU TestCases below so
    both backends are held to the exact same behavior, not just "doesn't
    crash". Not a TestCase itself (no test_ methods would run standalone)."""

    force_cpu = None  # set by subclasses

    def setUp(self):
        self.backend = GraphBackend(force_cpu=self.force_cpu)
        self.assertEqual(self.backend.use_gpu, not self.force_cpu)
        self.G = self.backend.load_edgelist(synthetic_edges())

    def test_pagerank_ranks_the_hub_highest(self):
        df = self.backend.pagerank(self.G)
        self.assertEqual(set(df.columns), {"wallet_id", "score"})
        top_wallet = df.iloc[0]["wallet_id"]
        self.assertEqual(top_wallet, 1, "wallet 1 has the most incoming edges/weight and should rank first")

    def test_louvain_separates_the_two_clusters(self):
        df, modularity = self.backend.louvain(self.G)
        self.assertEqual(set(df.columns), {"wallet_id", "community_label"})
        label_123 = set(df[df.wallet_id.isin([1, 2, 3])].community_label)
        label_45 = set(df[df.wallet_id.isin([4, 5])].community_label)
        self.assertTrue(label_123.isdisjoint(label_45), "the two disconnected groups should land in different communities")
        self.assertGreater(modularity, 0)

    def test_bfs_from_hub_reaches_everyone_in_its_component(self):
        df = self.backend.bfs(self.G, 1)
        reached = set(df.wallet_id)
        self.assertTrue({1, 2}.issubset(reached))  # 1 -> 2 is a direct edge
        # Both backends must agree on exactly who's reachable -- cugraph.bfs
        # returns a row per vertex with an int64-max sentinel for unreached
        # ones; GraphBackend.bfs() must filter those out on the GPU path so
        # callers never see a backend-specific shape here.
        self.assertEqual(reached, {1, 2})

    def test_bfs_predecessor_of_start_vertex_is_sentinel(self):
        df = self.backend.bfs(self.G, 1)
        start_row = df[df.wallet_id == 1].iloc[0]
        self.assertEqual(start_row.distance, 0)
        self.assertEqual(start_row.predecessor, -1)

    def test_pagerank_excluding_signed_edges_removes_that_wallets_outgoing_influence(self):
        """graph/audit.py's frameable/robust check for pagerank_threshold:
        masking out a wallet's signed (outgoing) edges before re-running
        PageRank. Run on both the CPU and GPU backends (GPU skipped
        automatically without RAPIDS) -- same assertion on both proves the
        masking isn't a backend-specific no-op."""
        edges_df = synthetic_edges()
        full = self.backend.pagerank(self.G)
        full_score_1 = float(full[full.wallet_id == 1].iloc[0].score)

        masked = self.backend.pagerank_excluding_signed_edges(edges_df, exclude_wallet_id=2)
        self.assertEqual(set(masked.columns), {"wallet_id", "score"})
        masked_score_1 = float(masked[masked.wallet_id == 1].iloc[0].score)

        # wallet 2 -> 1 (weight 5.0, wallet 1's single largest inbound edge)
        # is removed -- wallet 1's rank must measurably drop.
        self.assertLess(masked_score_1, full_score_1)


class TestGraphBackendCPU(_GraphBackendTests, unittest.TestCase):
    force_cpu = True


class TestPageRankConvergence(unittest.TestCase):
    """NetworkX converges on sum|x - x_last| < N * tol, so a fixed tol gets
    looser as the graph grows. That is what made the CPU and GPU backends
    disagree on 1,498 of snapshot 3's 4,551 pagerank_threshold flags, so the
    N-scaling is pinned here rather than left as a comment."""

    def test_tolerance_scales_with_vertex_count(self):
        self.assertAlmostEqual(backend.cpu_pagerank_tol(1), backend.PAGERANK_L1_TARGET)
        self.assertAlmostEqual(backend.cpu_pagerank_tol(1_000), backend.PAGERANK_L1_TARGET / 1_000)
        # The whole point: NetworkX multiplies tol by N again, so the
        # effective L1 budget is the same no matter how big the graph is.
        for n in (10, 10_000, 455_073):
            self.assertAlmostEqual(backend.cpu_pagerank_tol(n) * n, backend.PAGERANK_L1_TARGET)

    def test_empty_graph_does_not_divide_by_zero(self):
        self.assertEqual(backend.cpu_pagerank_tol(0), backend.PAGERANK_L1_TARGET)

    def test_cpu_path_asks_networkx_for_the_scaled_tolerance(self):
        """The tolerance only helps if it actually reaches NetworkX -- the
        original bug was a default nobody passed."""
        b = GraphBackend(force_cpu=True)
        G = b.load_edgelist(synthetic_edges())
        seen = {}
        real_pagerank = b._networkx.pagerank

        def spy(graph, **kwargs):
            seen.update(kwargs)
            return real_pagerank(graph, **kwargs)

        b._networkx = types.SimpleNamespace(
            pagerank=spy, PowerIterationFailedConvergence=b._networkx.PowerIterationFailedConvergence,
        )
        b.pagerank(G)
        self.assertEqual(seen["tol"], backend.cpu_pagerank_tol(G.number_of_nodes()))
        self.assertEqual(seen["max_iter"], backend.PAGERANK_MAX_ITER)
        self.assertEqual(seen["weight"], "edge_weight")

    def test_non_convergence_is_logged_and_falls_back_rather_than_raising(self):
        """A pipeline run must not die on a graph that won't converge, but it
        must not look converged either."""
        b = GraphBackend(force_cpu=True)
        G = b.load_edgelist(synthetic_edges())
        real_nx = b._networkx
        calls = []

        def stubborn(graph, **kwargs):
            calls.append(kwargs)
            if "tol" in kwargs:
                raise real_nx.PowerIterationFailedConvergence(kwargs["max_iter"])
            return real_nx.pagerank(graph, **kwargs)

        b._networkx = types.SimpleNamespace(
            pagerank=stubborn, PowerIterationFailedConvergence=real_nx.PowerIterationFailedConvergence,
        )
        with self.assertLogs("graph.backend", level="WARNING") as logged:
            df = b.pagerank(G)
        self.assertEqual(len(calls), 2, "should retry once without the tight tolerance")
        self.assertNotIn("tol", calls[1])
        self.assertIn("NOT comparable", "\n".join(logged.output))
        self.assertEqual(len(df), G.number_of_nodes())


@unittest.skipUnless(gpu_available(), "RAPIDS (cudf+cugraph) not installed -- expected off the GPU laptops")
class TestGraphBackendGPU(_GraphBackendTests, unittest.TestCase):
    force_cpu = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
