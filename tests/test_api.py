"""
Tests for api/main.py in mock mode -- this is the contract the frontend is
built against (docs/API_CONTRACT.md), so every shape here matters.

Runs against api/mock_data.json, not Postgres. Forced explicitly, not just
relying on TXGRAPH_API_MODE being unset -- a developer's own .env can (and,
on the machine this was found on, does) set it to "live" for their own
Postgres-backed dev work, which would otherwise leak in via
ingestion.config's load_dotenv() and silently point this suite at a real
database. Must be set before importing api.main (transitively imports
ingestion.config, which calls load_dotenv()) -- load_dotenv() doesn't
override an already-set env var, so this wins.
"""
import os

os.environ["TXGRAPH_API_MODE"] = "mock"

import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import main as api_main  # noqa: E402
from api.main import app, load_mock  # noqa: E402

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


class TestClusterMembership:
    """/cluster-membership backs both the frontend's dashboard load (one
    batch call replacing what used to be one request per cluster) and the
    desktop app's Rust layout engine (cluster-aware force layout, see
    src-tauri/src/commands.rs's fetch_cluster_membership) -- exercised on
    every real use of the app, but had no test coverage until now."""

    def test_maps_wallets_to_their_clusters(self):
        resp = client.get("/cluster-membership", params={"cluster_ids": "1"})
        assert resp.status_code == 200
        membership = resp.json()
        assert set(int(k) for k in membership.keys()) == {2, 3, 4}
        assert membership["2"] == [1]

    def test_unknown_cluster_id_returns_empty_mapping_not_error(self):
        resp = client.get("/cluster-membership", params={"cluster_ids": "9999"})
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_empty_cluster_ids_returns_empty_mapping(self):
        resp = client.get("/cluster-membership", params={"cluster_ids": ""})
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_rejects_non_integer_cluster_ids(self):
        resp = client.get("/cluster-membership", params={"cluster_ids": "1,abc"})
        assert resp.status_code == 422

    def test_unknown_id_mixed_with_real_id_does_not_drop_the_real_one(self):
        resp = client.get("/cluster-membership", params={"cluster_ids": "1,9999"})
        assert resp.status_code == 200
        assert set(int(k) for k in resp.json().keys()) == {2, 3, 4}


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
        self.closed = 0

    def cursor(self):
        return _FakeCursor(self._sink)

    def close(self):
        pass

    # get_conn() is now a context manager (pooled connections) -- these let
    # `with get_conn() as conn:` work against this fake the same way it
    # works against a real pooled connection.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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

    def test_cluster_membership(self, monkeypatch):
        query = self._live_query(monkeypatch, "/cluster-membership?cluster_ids=1")
        assert "cluster_member" in query
        assert "wallet_id" in query
        assert "cluster_id" in query


class TestUnhandledErrors:
    """An unexpected error (a DB blowing up, a bug) must become a clean,
    generic 500 -- never leak the real exception text, SQL, or a traceback
    into the response body. The real detail still needs to be findable
    somewhere (the log), just not handed to whoever's calling the API."""

    def test_unexpected_exception_returns_generic_500_not_the_real_error(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)

        class _ExplodingConn:
            def cursor(self):
                raise RuntimeError("SELECT password_hash FROM analyst -- should never reach a caller")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(api_main, "get_conn", lambda: _ExplodingConn())

        # TestClient's default (raise_server_exceptions=True) re-raises an
        # unhandled exception for debugging rather than returning the
        # formatted response -- exactly what we're testing here, so it
        # needs a client with that off to see what a real ASGI server (and
        # a real caller) would actually receive.
        no_raise_client = TestClient(app, raise_server_exceptions=False)
        resp = no_raise_client.get("/wallets")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "internal server error"}
        assert "password_hash" not in resp.text
        assert "RuntimeError" not in resp.text

    def test_known_http_exceptions_are_unaffected_by_the_generic_handler(self):
        # A real, existing 404 (mock mode, unknown wallet_id) must still
        # come through with its own specific detail message, not get
        # swallowed by the new catch-all handler.
        resp = client.get("/wallets/999999999")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "wallet not found"}


class TestConnectionPoolDiscardsBrokenConnections:
    """get_conn()'s whole reason to exist beyond a bare psycopg2.connect()
    call: a connection that dies mid-request (e.g. a Postgres restart) must
    be discarded, not handed back to the pool for the next request to trip
    over."""

    class _FakeConn:
        def __init__(self):
            self.closed = 0

    class _FakePool:
        def __init__(self, conn):
            self._conn = conn
            self.putconn_calls = []

        def getconn(self):
            return self._conn

        def putconn(self, conn, close=False):
            self.putconn_calls.append((conn, close))

    def test_successful_request_returns_connection_to_pool_without_closing(self, monkeypatch):
        conn = self._FakeConn()
        fake_pool = self._FakePool(conn)
        monkeypatch.setattr(api_main, "_get_pool", lambda: fake_pool)

        with api_main.get_conn() as c:
            assert c is conn

        assert fake_pool.putconn_calls == [(conn, False)]

    def test_operational_error_discards_the_connection(self, monkeypatch):
        import psycopg2

        conn = self._FakeConn()
        fake_pool = self._FakePool(conn)
        monkeypatch.setattr(api_main, "_get_pool", lambda: fake_pool)

        with pytest.raises(psycopg2.OperationalError):
            with api_main.get_conn() as c:
                raise psycopg2.OperationalError("server closed the connection unexpectedly")

        assert fake_pool.putconn_calls == [(conn, True)]

    def test_already_closed_connection_is_discarded_even_without_an_exception(self, monkeypatch):
        conn = self._FakeConn()
        conn.closed = 1  # e.g. the server closed it, no exception raised locally
        fake_pool = self._FakePool(conn)
        monkeypatch.setattr(api_main, "_get_pool", lambda: fake_pool)

        with api_main.get_conn():
            pass

        assert fake_pool.putconn_calls == [(conn, True)]


class TestGetPoolIsThreadSafe:
    """_get_pool()'s lazy init raced under real concurrent callers: FastAPI
    dispatches sync endpoints to a worker thread pool, so a burst of
    simultaneous first requests (e.g. the dashboard's initial batch of GET
    calls on load) could each observe `_pool is None`, each construct their
    own ThreadedConnectionPool, and overwrite the global -- a connection
    checked out from the losing instance then gets returned via the
    (different) winning instance, which raises psycopg2.pool.PoolError:
    trying to put unkeyed connection. Reproduced live via a real
    `cargo tauri dev` launch before _pool_lock was added."""

    def test_concurrent_first_calls_create_exactly_one_pool(self, monkeypatch):
        construct_count = 0
        count_lock = threading.Lock()

        class _FakePool:
            def __init__(self, minconn, maxconn, **kwargs):
                nonlocal construct_count
                with count_lock:
                    construct_count += 1
                # Widen the race window: without _pool_lock, every thread's
                # `if _pool is None` check passes before any of them finishes
                # constructing, so they'd all construct their own instance.
                time.sleep(0.02)

        monkeypatch.setattr(api_main, "_pool", None)
        monkeypatch.setattr(api_main.pg_pool, "ThreadedConnectionPool", _FakePool)

        barrier = threading.Barrier(20)

        def call():
            barrier.wait()
            api_main._get_pool()

        threads = [threading.Thread(target=call) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert construct_count == 1
