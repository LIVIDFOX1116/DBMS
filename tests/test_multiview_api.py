"""
Offline tests for the Multiview endpoints: /clusters/top-flagged and
/clusters/{id}/subgraph, plus the connected-expansion helper that decides
what a truncated panel actually shows.
"""
import os

os.environ["FRAMECHECK_API_MODE"] = "mock"
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

from unittest import mock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from api import main as api_main  # noqa: E402
from api.main import app, select_connected_subgraph  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


class TestSelectConnectedSubgraph:
    """Taking the top N by PageRank renders as a dust cloud: on the real
    26,594-member cluster 351299 it produced 76 disconnected fragments with
    22% of nodes having no edge to anything else shown."""

    def test_expands_along_edges_instead_of_taking_the_top_ranked(self):
        # 1 and 2 rank highest but sit alone; 3-6 are a connected chain.
        ranked = [1, 2, 3, 4, 5, 6]
        adjacency = {3: {4}, 4: {3, 5}, 5: {4, 6}, 6: {5}}
        chosen = select_connected_subgraph(ranked, adjacency, limit=4)
        # Seeds at 1 (isolated), then 2 (isolated), then reaches the chain.
        assert chosen[:2] == [1, 2]
        assert set(chosen[2:]) <= {3, 4, 5, 6}

    def test_a_connected_component_is_taken_whole_before_moving_on(self):
        ranked = [1, 2, 3, 4, 5]
        adjacency = {1: {2}, 2: {1, 3}, 3: {2}, 4: {5}, 5: {4}}
        chosen = select_connected_subgraph(ranked, adjacency, limit=3)
        assert sorted(chosen) == [1, 2, 3]  # not [1, 2, 4] or similar

    def test_prefers_the_better_ranked_neighbour_at_each_step(self):
        # From hub 1, neighbours 5 and 2 are both available; 2 ranks higher.
        ranked = [1, 2, 3, 4, 5]
        adjacency = {1: {5, 2}, 2: {1}, 5: {1}}
        assert select_connected_subgraph(ranked, adjacency, limit=2) == [1, 2]

    def test_stops_at_the_limit(self):
        ranked = list(range(20))
        adjacency = {i: {i + 1} for i in range(19)}
        adjacency.update({i + 1: {i} for i in range(19)})
        assert len(select_connected_subgraph(ranked, adjacency, limit=7)) == 7

    def test_handles_a_cluster_with_no_edges_at_all(self):
        # Louvain can emit singleton clusters; it must not hang or crash.
        assert select_connected_subgraph([1, 2, 3], {}, limit=2) == [1, 2]

    def test_empty_inputs(self):
        assert select_connected_subgraph([], {}, limit=5) == []

    def test_never_repeats_a_wallet(self):
        ranked = [1, 2, 3, 4]
        adjacency = {1: {2, 3}, 2: {1, 3}, 3: {1, 2, 4}, 4: {3}}
        chosen = select_connected_subgraph(ranked, adjacency, limit=4)
        assert len(chosen) == len(set(chosen))


class TestTopFlaggedRouting:
    def test_top_flagged_is_not_shadowed_by_the_cluster_id_route(self):
        # FastAPI matches in declaration order, so if /clusters/{cluster_id}
        # were declared first it would try to parse "top-flagged" as an int
        # and 422 before this handler ran.
        resp = client.get("/clusters/top-flagged")
        assert resp.status_code == 200, resp.text

    def test_limit_is_capped_at_eight_panels(self):
        assert client.get("/clusters/top-flagged?limit=9").status_code == 422
        assert client.get("/clusters/top-flagged?limit=0").status_code == 422
        assert client.get("/clusters/top-flagged?limit=8").status_code == 200

    def test_mock_mode_returns_a_list_not_an_error(self):
        assert isinstance(client.get("/clusters/top-flagged").json(), list)


class TestTopFlaggedLive:
    def _conn(self, rows, fetchone_values):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = fetchone_values
        cur.fetchall.return_value = rows
        cur.description = [("cluster_id",), ("community_label",), ("member_count",),
                           ("cluster_type",), ("risk_score",), ("flagged_wallets",),
                           ("flag_count",), ("frameable_count",), ("audited_count",)]
        return conn

    def test_clusters_and_flags_are_joined_through_the_snapshot(self, monkeypatch):
        # cluster.run_id is a Louvain run and flag.run_id is a detector run:
        # different analysis_run rows. Matching run_id to run_id silently
        # returns nothing, so the join must go through snapshot_id.
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        conn = self._conn([], [(2,), (11,)])
        monkeypatch.setattr(api_main, "get_conn", lambda: _ctx(conn))
        monkeypatch.setattr(api_main, "resolve_latest_run_ids", lambda cur, snap: [5, 6])
        client.get("/clusters/top-flagged?run_id=99")
        queries = [c.args[0] for c in conn.cursor.return_value.__enter__.return_value.execute.call_args_list]
        assert any("snapshot_id" in q for q in queries)
        assert any("algorithm_name" in q for q in queries)

    def test_frameable_share_is_null_when_nothing_in_the_cluster_was_audited(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        conn = self._conn([(1, 7, 10, None, None, 4, 5, 0, 0)], [(2,), (11,)])
        monkeypatch.setattr(api_main, "get_conn", lambda: _ctx(conn))
        monkeypatch.setattr(api_main, "resolve_latest_run_ids", lambda cur, snap: [5])
        row = client.get("/clusters/top-flagged?run_id=99").json()[0]
        assert row["frameable_share"] is None  # not 0.0, which would read as "none are frameable"

    def test_unknown_run_is_404(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        conn = self._conn([], [None])
        monkeypatch.setattr(api_main, "get_conn", lambda: _ctx(conn))
        assert client.get("/clusters/top-flagged?run_id=999999").status_code == 404


class TestSubgraphEndpoint:
    def test_limit_bounds_are_enforced(self):
        assert client.get("/clusters/1/subgraph?limit=0").status_code == 422
        assert client.get("/clusters/1/subgraph?limit=99999").status_code == 422

    def test_unknown_cluster_is_404(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        monkeypatch.setattr(api_main, "get_conn", lambda: _ctx(conn))
        resp = client.get("/clusters/999999/subgraph")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "no such cluster"}

    def test_mock_mode_shape_is_the_same_as_live(self):
        body = client.get("/clusters/1/subgraph").json()
        assert set(body) >= {"nodes", "edges", "truncated"}


class _ctx:
    """Minimal stand-in for api.main.get_conn()'s context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False
