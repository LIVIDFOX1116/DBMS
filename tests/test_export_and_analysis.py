"""
Offline tests for graph/export_snapshot.py (Parquet round-trip, no DB needed)
and graph/run_analysis.py's bookkeeping (fake connection, no DB needed). The
live Postgres read (build_edge_dataframe / pd.read_sql) and the live GPU
algorithms are exercised on the real laptops, not here -- see README.md.
"""
import ast
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

from graph.export_snapshot import write_parquet
from graph.run_analysis import (
    persist_vertex_metric, persist_clusters, start_run, finish_run,
    compute_internal_edges, classify_clusters,
)


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
        self._last_many = []
        # execute_values() itself (not just our mogrify stub above) reaches
        # into cur.connection.encoding to encode the base SQL string -- a
        # bare object with that one attribute is enough to satisfy it.
        self.connection = type("_FakeConnEncoding", (), {"encoding": "UTF8"})()

    def mogrify(self, template, args):
        """Stands in for psycopg2's real mogrify, which execute_values calls
        internally to build its multi-row VALUES literal -- repr() per field
        is a faithful enough stand-in since this fake never touches a real
        database; _extract_rows below parses the exact same repr() text back
        out with ast.literal_eval. template arrives as bytes (execute_values
        derives it from the already-encoded base SQL)."""
        rendered = template.decode()
        for value in args:
            rendered = rendered.replace("%s", repr(value), 1)
        return rendered.encode()

    def _extract_rows(self, query):
        """execute_values composes one query with a literal VALUES
        (...),(...),... list (via mogrify above) instead of passing a
        params tuple -- recover the original rows the same way they were
        serialized, since nothing else will do it for this fake."""
        match = re.search(r"VALUES\s+(.+?)(?:\s+ON CONFLICT|\s+RETURNING|$)", query, re.IGNORECASE | re.DOTALL)
        return ast.literal_eval("[" + match.group(1) + "]")

    def execute(self, query, params=()):
        if isinstance(query, bytes):
            query = query.decode()  # execute_values composes+encodes the final multi-row SQL itself
        q = " ".join(query.split())
        if q.startswith("SELECT algorithm_id"):
            (name,) = params
            self._last = (self.state["algorithms"][name],)
        elif q.startswith("INSERT INTO analysis_run"):
            run_id = len(self.state["runs"]) + 1
            self.state["runs"][run_id] = {"status": "RUNNING", "params": params}
            self._last = (run_id,)
        elif q.startswith("UPDATE analysis_run"):
            status, runtime_ms, gpu_device, backend_library_version, run_id = params
            self.state["runs"][run_id].update(
                status=status, runtime_ms=runtime_ms, gpu_device=gpu_device,
                backend_library_version=backend_library_version,
            )
            self._last = None
        elif q.startswith("INSERT INTO vertex_metric"):
            self.state["vertex_metrics"].extend(self._extract_rows(q))
            self._last = None
        elif q.startswith("INSERT INTO cluster "):
            rows = self._extract_rows(q)  # each: (run_id, community_label, member_count, internal_edges, modularity)
            returned = []
            for row in rows:
                cluster_id = len(self.state["clusters"]) + 1
                self.state["clusters"][cluster_id] = row
                returned.append((cluster_id, row[1]))  # row[1] = community_label, per the real RETURNING clause
            self._last_many = returned
            self._last = None
        elif q.startswith("INSERT INTO cluster_member"):
            self.state["cluster_members"].extend(self._extract_rows(q))
            self._last = None
        elif q.startswith("SELECT cluster_id, member_count, internal_edges"):
            (run_id,) = params
            self._last_many = [
                (cid, c["member_count"], c["internal_edges"])
                for cid, c in self.state["clusters_full"].items()
                if c["run_id"] == run_id
            ]
            self._last = None
        elif q.startswith("UPDATE cluster SET cluster_type"):
            risk_score, cluster_id = params
            self.state["clusters_full"][cluster_id]["cluster_type"] = "SYBIL"
            self.state["clusters_full"][cluster_id]["risk_score"] = risk_score
            self._last = None
        elif q.startswith("INSERT INTO alert"):
            self.state["alerts"].append(params)
            self._last = None
        else:
            raise AssertionError(f"FakeCursor doesn't know: {q[:60]}")

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last_many

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self):
        self.state = {
            "algorithms": {"pagerank": 1, "louvain": 2, "bfs": 3},
            "runs": {}, "vertex_metrics": [], "clusters": {}, "cluster_members": [],
            "clusters_full": {}, "alerts": [],
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

    def test_persist_clusters_maps_members_by_community_label_not_row_order(self):
        """The real risk execute_values introduces: Postgres makes no
        row-order guarantee for a multi-row INSERT...RETURNING, so
        persist_clusters must map the returned cluster_id back to each
        wallet by community_label, never by assuming RETURNING rows came
        back in the same order the VALUES list was given in."""
        conn = FakeConnection()
        run_id = start_run(conn, snapshot_id=1, algorithm_name="louvain")
        df = pd.DataFrame([
            {"wallet_id": 10, "community_label": 5},
            {"wallet_id": 20, "community_label": 5},
            {"wallet_id": 30, "community_label": 2},
            {"wallet_id": 40, "community_label": 2},
            {"wallet_id": 50, "community_label": 2},
        ])
        persist_clusters(conn, run_id, df, modularity=0.5)

        label_to_cluster_id = {row[1]: cid for cid, row in conn.state["clusters"].items()}
        members_of = {}
        for cluster_id, wallet_id in conn.state["cluster_members"]:
            members_of.setdefault(cluster_id, set()).add(wallet_id)

        self.assertEqual(members_of[label_to_cluster_id[5]], {10, 20})
        self.assertEqual(members_of[label_to_cluster_id[2]], {30, 40, 50})


class TestComputeInternalEdges(unittest.TestCase):
    def test_counts_only_edges_within_the_same_community(self):
        cluster_df = pd.DataFrame([
            {"wallet_id": 1, "community_label": 0},
            {"wallet_id": 2, "community_label": 0},
            {"wallet_id": 3, "community_label": 0},
            {"wallet_id": 4, "community_label": 1},
        ])
        edges_df = pd.DataFrame([
            {"src_wallet_id": 1, "dst_wallet_id": 2},  # within community 0
            {"src_wallet_id": 2, "dst_wallet_id": 3},  # within community 0
            {"src_wallet_id": 3, "dst_wallet_id": 4},  # crosses communities -- not counted
        ])
        counts = compute_internal_edges(cluster_df, edges_df)
        self.assertEqual(counts, {0: 2})  # community 1 has no internal edges at all


class TestClassifyClusters(unittest.TestCase):
    def _seed(self, conn, clusters):
        conn.state["clusters_full"] = clusters

    def test_flags_star_shaped_cluster_as_sybil(self):
        conn = FakeConnection()
        self._seed(conn, {
            1: {"run_id": 7, "member_count": 10, "internal_edges": 9},  # star: 9 = 10-1 edges
        })
        flagged = classify_clusters(conn, run_id=7)
        self.assertEqual(flagged, 1)
        self.assertEqual(conn.state["clusters_full"][1]["cluster_type"], "SYBIL")
        self.assertEqual(len(conn.state["alerts"]), 1)

    def test_skips_cluster_below_minimum_size(self):
        conn = FakeConnection()
        self._seed(conn, {
            1: {"run_id": 7, "member_count": 3, "internal_edges": 2},  # too small to be meaningful
        })
        flagged = classify_clusters(conn, run_id=7)
        self.assertEqual(flagged, 0)
        self.assertNotIn("cluster_type", conn.state["clusters_full"][1])
        self.assertEqual(conn.state["alerts"], [])

    def test_skips_densely_connected_cluster(self):
        conn = FakeConnection()
        self._seed(conn, {
            # ratio 2.0 -- much denser than a star, not the fan-out pattern this heuristic targets
            1: {"run_id": 7, "member_count": 10, "internal_edges": 20},
        })
        flagged = classify_clusters(conn, run_id=7)
        self.assertEqual(flagged, 0)

    def test_larger_star_cluster_gets_high_severity(self):
        conn = FakeConnection()
        self._seed(conn, {
            1: {"run_id": 7, "member_count": 25, "internal_edges": 24},
        })
        classify_clusters(conn, run_id=7)
        run_id, cluster_id, severity, confidence = conn.state["alerts"][0]
        self.assertEqual(severity, "HIGH")

    def test_only_classifies_clusters_from_the_given_run(self):
        conn = FakeConnection()
        self._seed(conn, {
            1: {"run_id": 7, "member_count": 10, "internal_edges": 9},
            2: {"run_id": 8, "member_count": 10, "internal_edges": 9},  # different run -- not touched
        })
        flagged = classify_clusters(conn, run_id=7)
        self.assertEqual(flagged, 1)
        self.assertNotIn("cluster_type", conn.state["clusters_full"][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
