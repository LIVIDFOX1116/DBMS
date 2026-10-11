"""
Offline tests for the automatic-ingestion half of Feature 1: the call
budget, the plan probe's refusal to overclaim, crawl()'s progress/cancel
hooks, and the routes.

No network and no Postgres -- the Etherscan client is faked, and the one
test that exercises crawl() hands it a fake client and a fake connection,
the same way tests/test_ingest_subgraph.py already does.
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

import requests
from fastapi.testclient import TestClient

from api import jobs, main
from ingestion import ingest_subgraph
from ingestion.etherscan_client import BudgetExhausted, EtherscanClient


class TestCallBudget(unittest.TestCase):
    def test_no_budget_means_unlimited_and_is_distinct_from_zero_left(self):
        client = EtherscanClient(api_key="k")
        self.assertIsNone(client.max_calls)
        self.assertIsNone(client.calls_remaining(), "no budget must not look like 'none left'")

    def test_calls_are_counted_and_the_budget_stops_the_next_one(self):
        client = EtherscanClient(api_key="k", requests_per_second=1000, max_calls=2)
        payload = {"status": "1", "result": []}
        with mock.patch("ingestion.etherscan_client.requests.get") as get:
            get.return_value = mock.Mock(status_code=200, json=lambda: payload,
                                         raise_for_status=lambda: None)
            client._get({"module": "account", "action": "txlist"})
            client._get({"module": "account", "action": "txlist"})
            self.assertEqual(client.calls_made, 2)
            self.assertEqual(client.calls_remaining(), 0)
            with self.assertRaises(BudgetExhausted):
                client._get({"module": "account", "action": "txlist"})
        self.assertEqual(get.call_count, 2, "the over-budget call must not reach the network")

    def test_budget_exhausted_is_not_an_etherscan_error(self):
        """The job treats it as a clean stop, so it must not be catchable as
        a transport/API failure."""
        from ingestion.etherscan_client import EtherscanError
        self.assertFalse(issubclass(BudgetExhausted, EtherscanError))


class TestPlanProbe(unittest.TestCase):
    """Etherscan only reports an allowance for paid keys. Every non-answer
    has to degrade to None so the app falls back to the configured rate
    instead of claiming to know a plan."""

    def _probe_returning(self, **mock_kwargs):
        client = EtherscanClient(api_key="k", requests_per_second=1000)
        with mock.patch("ingestion.etherscan_client.requests.get", **mock_kwargs):
            return client.get_api_limit()

    def test_a_free_key_error_response_reports_nothing(self):
        self.assertIsNone(self._probe_returning(return_value=mock.Mock(
            raise_for_status=lambda: None,
            json=lambda: {"status": "0", "message": "NOTOK", "result": "Invalid API Key"})))

    def test_an_unexpected_shape_reports_nothing(self):
        self.assertIsNone(self._probe_returning(return_value=mock.Mock(
            raise_for_status=lambda: None, json=lambda: {"status": "1", "result": "surprise"})))

    def test_a_network_failure_reports_nothing(self):
        self.assertIsNone(self._probe_returning(side_effect=requests.ConnectionError("offline")))

    def test_unparseable_json_reports_nothing(self):
        def boom():
            raise ValueError("not json")
        self.assertIsNone(self._probe_returning(return_value=mock.Mock(
            raise_for_status=lambda: None, json=boom)))

    def test_a_real_answer_is_passed_through(self):
        limit = {"creditLimit": "200000", "creditsUsed": "17", "creditsAvailable": "199983"}
        self.assertEqual(self._probe_returning(return_value=mock.Mock(
            raise_for_status=lambda: None, json=lambda: {"status": "1", "result": limit})), limit)


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, *a, **k):
        self.executed.append(a)

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class TestCrawlHooks(unittest.TestCase):
    """crawl() runs for minutes to hours, so Cancel has to take effect
    mid-hop, and progress has to arrive per address rather than per hop."""

    def setUp(self):
        self.neighbours = {f"0x{i:040x}" for i in range(1, 6)}
        patcher = mock.patch.object(ingest_subgraph, "process_address_with_retry",
                                    side_effect=lambda cur, client, addr: set(self.neighbours))
        self.process = patcher.start()
        self.addCleanup(patcher.stop)

    def test_progress_is_reported_once_per_address(self):
        seen = []
        ingest_subgraph.crawl("0x" + "ab" * 20, hop_count=1, client=mock.Mock(calls_made=3),
                              conn=FakeConn(), resume=False, progress=seen.append)
        self.assertEqual(len(seen), 6, "the seed plus its five neighbours")
        self.assertEqual([s["addresses_processed"] for s in seen], [1, 2, 3, 4, 5, 6])
        self.assertEqual(seen[0]["calls_made"], 3, "the client's call count travels with progress")

    def test_cancel_stops_mid_hop_and_says_so(self):
        stop_after = 2
        seen = []

        def progress(stats):
            seen.append(stats)

        stats = ingest_subgraph.crawl("0x" + "ab" * 20, hop_count=3, client=mock.Mock(calls_made=0),
                                      conn=FakeConn(), resume=False, progress=progress,
                                      should_stop=lambda: len(seen) >= stop_after)
        self.assertTrue(stats["stopped_early"])
        self.assertEqual(stats["addresses_processed"], stop_after,
                         "it must stop at the next address, not at the end of the hop")

    def test_work_done_before_a_stop_is_committed(self):
        conn = FakeConn()
        ingest_subgraph.crawl("0x" + "ab" * 20, hop_count=2, client=mock.Mock(calls_made=0),
                              conn=conn, resume=False, should_stop=lambda: True)
        self.assertEqual(conn.commits, 1, "the address it did process must be committed, not rolled back")


class TestIngestRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(main._runtime_state.__setitem__, "etherscan_api_key",
                        main._runtime_state["etherscan_api_key"])

    def test_ingestion_is_refused_in_demo_mode(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(main, "using_mock", return_value=True):
            res = self.client.post("/ingest/start", json={"hops": 1})
        self.assertEqual(res.status_code, 409)
        self.assertIn("live mode", res.json()["detail"])

    def test_ingestion_is_refused_without_a_key(self):
        main._runtime_state["etherscan_api_key"] = ""
        with mock.patch.object(main, "using_mock", return_value=False):
            res = self.client.post("/ingest/start", json={"hops": 1})
        self.assertEqual(res.status_code, 409)
        self.assertIn("API key", res.json()["detail"])

    def test_an_absurd_hop_count_is_rejected(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(main, "using_mock", return_value=False):
            for hops in (-1, 4, 50):
                res = self.client.post("/ingest/start", json={"hops": hops})
                self.assertEqual(res.status_code, 422, f"hops={hops}")

    def test_a_zero_budget_is_rejected_rather_than_meaning_unlimited(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(main, "using_mock", return_value=False):
            res = self.client.post("/ingest/start", json={"hops": 1, "max_calls": 0})
        self.assertEqual(res.status_code, 422)

    def test_the_api_key_never_comes_back_in_a_job_response(self):
        main._runtime_state["etherscan_api_key"] = "super-secret-key"
        fake_job = jobs.Job(job_id="abc", job_type="ingest", snapshot_id=0, seed_wallet_id=0,
                            params={"address": "0xabc", "hops": 1, "api_key": "super-secret-key"})
        with mock.patch.object(main, "using_mock", return_value=False), \
             mock.patch.object(jobs, "start", return_value=fake_job), \
             mock.patch.object(jobs, "get_job", return_value=fake_job), \
             mock.patch.object(jobs, "list_jobs", return_value=[fake_job.as_dict()]):
            started = self.client.post("/ingest/start", json={"hops": 1})
            fetched = self.client.get("/jobs/abc")
            listed = self.client.get("/jobs")
        for res in (started, fetched, listed):
            self.assertEqual(res.status_code, 200)
            self.assertNotIn("super-secret-key", res.text)
            self.assertNotIn("api_key", res.text)
        self.assertEqual(started.json()["params"]["address"], "0xabc", "the rest of the params stay")

    def test_the_plan_is_reported_as_configured_when_etherscan_will_not_say(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(EtherscanClient, "get_api_limit", return_value=None):
            res = self.client.get("/ingest/plan")
        body = res.json()
        self.assertEqual(body["source"], "configured")
        self.assertIsNone(body["reported_by_etherscan"])
        self.assertGreater(body["requests_per_second"], 0)

    def test_the_plan_is_reported_as_etherscans_when_it_does_say(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(EtherscanClient, "get_api_limit",
                               return_value={"creditLimit": "200000"}):
            body = self.client.get("/ingest/plan").json()
        self.assertEqual(body["source"], "etherscan")
        self.assertEqual(body["reported_by_etherscan"]["creditLimit"], "200000")

    def test_a_failing_probe_does_not_break_the_page(self):
        main._runtime_state["etherscan_api_key"] = "k"
        with mock.patch.object(EtherscanClient, "get_api_limit", side_effect=RuntimeError("boom")):
            res = self.client.get("/ingest/plan")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["source"], "configured")

    def test_no_key_means_no_probe_at_all(self):
        main._runtime_state["etherscan_api_key"] = ""
        with mock.patch.object(EtherscanClient, "get_api_limit") as probe:
            body = self.client.get("/ingest/plan").json()
        probe.assert_not_called()
        self.assertFalse(body["api_key_set"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
