"""
Exercises the CPU (pandas + NetworkX) path of GraphBackend on a small
synthetic edge list. This is the path that actually runs in the sandbox and
on the Zenbook; the GPU path runs the exact same test suite unmodified on the
GTX 1650 / RTX 4070 laptops once RAPIDS is installed (gpu_available()
switches automatically -- nothing here is CPU-specific except the import
availability check).
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
from graph.backend import GraphBackend


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


class TestGraphBackendCPU(unittest.TestCase):
    def setUp(self):
        self.backend = GraphBackend(force_cpu=True)
        self.assertFalse(self.backend.use_gpu)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
