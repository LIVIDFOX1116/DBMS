"""
Offline tests for graph/export_snapshot.py (Parquet round-trip, no DB needed)
and graph/run_analysis.py's bookkeeping (fake connection, no DB needed). The
live Postgres read (build_edge_dataframe / pd.read_sql) and the live GPU
algorithms are exercised on the real laptops, not here -- see README.md.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

from graph.export_snapshot import write_parquet
from graph.run_analysis import persist_vertex_metric, persist_clusters, start_run, finish_run


def sample_edge_df():
    return pd.DataFrame([
        {"src_wallet_id": 1, "dst_wallet_id": 2, "edge_weight": 10.0, "transfer_count": 2,
         "tx_hashes": ["0xaaa", "0xbbb"]},
        {"src_wallet_id": 2, "dst_wallet_id": 3, "edge_weight": 5.0, "transfer_count": 1,
         "tx_hashes": ["0xccc"]},
    ])


class TestExportSnapshot(unittest.TestCase):
    def test_write_parquet_roundtrip_and_checksum(self):
        edge_df = sample_edge_df()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = pathlib.Path(tmp) / "test_snapshot.parquet"
            checksum = write_parquet(edge_df, out_path)

            self.assertTrue(out_path.exists())
            self.assertEqual(len(checksum), 64)  # sha256 hex digest length

            reloaded = pd.read_parquet(out_path)
            self.assertNotIn("tx_hashes", reloaded.columns)  # dropped deliberately, see export_snapshot.py
            self.assertEqual(list(reloaded.src_wallet_id), [1, 2])
            self.assertEqual(list(reloaded.edge_weight), [10.0, 5.0])


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self._last = None

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT algorithm_id"):
            (name,) = params
            self._last = (self.state["algorithms"][name],)
        elif q.startswith("INSERT INTO analysis_run"):
            run_id = len(self.state["runs"]) + 1
            self.state["runs"][run_id] = {"status": "RUNNING", "params": params}
            self._last = (run_id,)
        elif q.startswith("UPDATE analysis_run"):
            status, runtime_ms, gpu_device, run_id = params
            self.state["runs"][run_id].update(status=status, runtime_ms=runtime_ms, gpu_device=gpu_device)
            self._last = None
        elif q.startswith("INSERT INTO vertex_metric"):
            self.state["vertex_metrics"].append(params)
            self._last = None
        elif q.startswith("INSERT INTO cluster "):
            cluster_id = len(self.state["clusters"]) + 1
            self.state["clusters"][cluster_id] = params
            self._last = (cluster_id,)
        elif q.startswith("INSERT INTO cluster_member"):
            self.state["cluster_members"].append(params)
            self._last = None
        else:
            raise AssertionError(f"FakeCursor doesn't know: {q[:60]}")

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self):
        self.state = {
            "algorithms": {"pagerank": 1, "louvain": 2, "bfs": 3},
            "runs": {}, "vertex_metrics": [], "clusters": {}, "cluster_members": [],
        }
        self.committed = 0

    def cursor(self):
        return FakeCursor(self.state)

    def commit(self):
        self.committed += 1


class TestRunAnalysisBookkeeping(unittest.TestCase):
    def test_run_lifecycle_and_vertex_metric_persistence(self):
        conn = FakeConnection()
        run_id = start_run(conn, snapshot_id=1, algorithm_name="pagerank")
        self.assertEqual(conn.state["runs"][run_id]["status"], "RUNNING")

        df = pd.DataFrame([{"wallet_id": 1, "score": 0.9}, {"wallet_id": 2, "score": 0.1}])
        persist_vertex_metric(conn, run_id, df, "pagerank", "score")
        self.assertEqual(len(conn.state["vertex_metrics"]), 2)

        finish_run(conn, run_id, runtime_ms=123, backend_name="pandas+networkx (CPU)")
        self.assertEqual(conn.state["runs"][run_id]["status"], "DONE")
        self.assertEqual(conn.state["runs"][run_id]["runtime_ms"], 123)

    def test_persist_clusters_groups_by_label(self):
        conn = FakeConnection()
        run_id = start_run(conn, snapshot_id=1, algorithm_name="louvain")
        df = pd.DataFrame([
            {"wallet_id": 1, "community_label": 0},
            {"wallet_id": 2, "community_label": 0},
            {"wallet_id": 3, "community_label": 1},
        ])
        persist_clusters(conn, run_id, df, modularity=0.42)

        self.assertEqual(len(conn.state["clusters"]), 2)  # two distinct community labels
        self.assertEqual(len(conn.state["cluster_members"]), 3)  # three wallets total


if __name__ == "__main__":
    unittest.main(verbosity=2)
