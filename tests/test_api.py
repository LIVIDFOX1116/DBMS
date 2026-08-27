"""
Tests for api/main.py in mock mode -- this is the contract the frontend is
built against (docs/API_CONTRACT.md), so every shape here matters.

Runs against api/mock_data.json, not Postgres -- TXGRAPH_API_MODE is unset,
which defaults to "mock" (api/main.py:using_mock()).
"""
from fastapi.testclient import TestClient

from api import main as api_main
from api.main import app, load_mock

client = TestClient(app)


class TestHealth:
    def test_health_reports_mock_mode(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "mode": "mock"}


class TestWallets:
    def test_list_wallets_returns_all_mock_wallets(self):
        resp = client.get("/wallets")
        assert resp.status_code == 200
        wallets = resp.json()
        assert len(wallets) == 6
        assert {"wallet_id", "address", "address_type", "risk_score", "tx_count"} <= wallets[0].keys()

    def test_list_wallets_respects_limit(self):
        resp = client.get("/wallets", params={"limit": 2})
        assert len(resp.json()) == 2

    def test_get_wallet_by_id(self):
        resp = client.get("/wallets/1")
        assert resp.status_code == 200
        assert resp.json()["address"] == "0xc8a65fadf0e0ddaf421f28feab69bf6e2e589963"

    def test_get_wallet_404_for_unknown_id(self):
        resp = client.get("/wallets/9999")
        assert resp.status_code == 404


class TestClusters:
    def test_list_clusters_excludes_members_field(self):
        resp = client.get("/clusters")
        assert resp.status_code == 200
        clusters = resp.json()
        assert len(clusters) == 1
        assert "members" not in clusters[0]
        assert clusters[0]["cluster_type"] == "SYBIL"

    def test_cluster_members_returns_member_list(self):
        resp = client.get("/clusters/1/members")
        assert resp.status_code == 200
        members = resp.json()
        assert len(members) == 3
        assert {"wallet_id", "address", "membership_score", "is_core_node"} <= members[0].keys()

    def test_cluster_members_404_for_unknown_cluster(self):
        resp = client.get("/clusters/9999/members")
        assert resp.status_code == 404

    def test_get_cluster_by_id(self):
        resp = client.get("/clusters/1")
        assert resp.status_code == 200
        cluster = resp.json()
        assert cluster["cluster_type"] == "SYBIL"
        assert "members" not in cluster

    def test_get_cluster_404_for_unknown_id(self):
        resp = client.get("/clusters/9999")
        assert resp.status_code == 404


class TestAlerts:
    def test_list_alerts_returns_all(self):
        resp = client.get("/alerts")
        assert len(resp.json()) == 2

    def test_list_alerts_filters_by_status(self):
        resp = client.get("/alerts", params={"status": "NEW"})
        alerts = resp.json()
        assert len(alerts) == 2
        assert all(a["review_status"] == "NEW" for a in alerts)

    def test_list_alerts_empty_for_unmatched_status(self):
        resp = client.get("/alerts", params={"status": "DISMISSED"})
        assert resp.json() == []


class TestSnapshots:
    def test_list_snapshots(self):
        resp = client.get("/snapshots")
        assert resp.status_code == 200
        snapshots = resp.json()
        assert len(snapshots) == 1
        assert snapshots[0]["snapshot_name"] == "polynetwork_2hop_v1"

    def test_snapshot_edges_returns_edge_list(self):
        resp = client.get("/snapshots/1/edges")
        assert resp.status_code == 200
        edges = resp.json()
        assert len(edges) == 7
        assert {"src_wallet_id", "dst_wallet_id", "edge_weight", "transfer_count"} <= edges[0].keys()

    def test_snapshot_edges_respects_limit(self):
        resp = client.get("/snapshots/1/edges", params={"limit": 2})
        assert len(resp.json()) == 2

    def test_snapshot_edges_empty_for_unknown_snapshot(self):
        resp = client.get("/snapshots/9999/edges")
        assert resp.json() == []


class TestRunMetrics:
    def test_pagerank_metrics(self):
        resp = client.get("/runs/1/metrics", params={"metric_name": "pagerank"})
        metrics = resp.json()
        assert len(metrics) == 6
        assert metrics[0]["rank_position"] == 1

    def test_bfs_distance_metrics(self):
        resp = client.get("/runs/1/metrics", params={"metric_name": "bfs_distance_from_1"})
        assert len(resp.json()) == 5

    def test_unknown_run_returns_empty_list_not_error(self):
        resp = client.get("/runs/9999/metrics")
        assert resp.status_code == 200
        assert resp.json() == []


class _FakeCursor:
    """Records the query text instead of touching a real database. rows_as_dicts()
    calls .description and .fetchall() on the way out, so both need to work even
    though there's no real result set -- an empty one is fine, we only care what
    SQL was asked for, not what it would return."""
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self._sink.append(" ".join(query.split()))

    def fetchall(self):
        return []

    def fetchone(self):
        # Truthy and non-empty so single-item GET endpoints (which check
        # `if not row: raise 404`) proceed past that check -- with description
        # empty, dict(zip(cols, row)) still comes out {}, which is all we need
        # since we only care what query text was issued, not the response body.
        return (1,)

    @property
    def description(self):
        return []


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)

    def close(self):
        pass


class TestLiveModeQueryShapes:
    """We can't spin up a live Postgres in CI, but every live-mode SQL query
    should ask for the same columns mock mode returns for the same endpoint --
    api/main.py's whole design point is that the two modes are interchangeable
    for callers (see its module docstring). This is exactly the bug class that
    let /clusters silently drop modularity_score in live mode while mock kept
    it, undetected until the endpoint was actually exercised against real
    Postgres data for the first time."""

    def _live_query(self, monkeypatch, path):
        queries = []
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        monkeypatch.setattr(api_main, "get_conn", lambda: _FakeConn(queries))
        resp = client.get(path)
        assert resp.status_code == 200
        assert queries, f"no query captured for {path}"
        return queries[0]

    def test_clusters_by_run_id(self, monkeypatch):
        query = self._live_query(monkeypatch, "/clusters?run_id=1")
        mock_keys = set(load_mock()["clusters"][0].keys()) - {"members"}
        missing = [k for k in mock_keys if k not in query]
        assert not missing, f"live /clusters?run_id= query is missing columns: {missing}"

    def test_clusters_unfiltered(self, monkeypatch):
        query = self._live_query(monkeypatch, "/clusters")
        mock_keys = set(load_mock()["clusters"][0].keys()) - {"members"}
        missing = [k for k in mock_keys if k not in query]
        assert not missing, f"live /clusters query is missing columns: {missing}"

    def test_cluster_by_id(self, monkeypatch):
        query = self._live_query(monkeypatch, "/clusters/1")
        mock_keys = set(load_mock()["clusters"][0].keys()) - {"members"}
        missing = [k for k in mock_keys if k not in query]
        assert not missing, f"live /clusters/{{id}} query is missing columns: {missing}"

    def test_wallets(self, monkeypatch):
        query = self._live_query(monkeypatch, "/wallets")
        mock_keys = set(load_mock()["wallets"][0].keys())
        missing = [k for k in mock_keys if k not in query]
        assert not missing, f"live /wallets query is missing columns: {missing}"

    def test_snapshots(self, monkeypatch):
        query = self._live_query(monkeypatch, "/snapshots")
        mock_keys = set(load_mock()["snapshots"][0].keys())
        missing = [k for k in mock_keys if k not in query]
        assert not missing, f"live /snapshots query is missing columns: {missing}"
