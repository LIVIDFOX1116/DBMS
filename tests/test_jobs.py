"""
Offline tests for api/jobs.py -- the UI's "run the analysis" path.

No Postgres: the detector/audit functions and the connection are both
substituted. What is tested is the job machinery itself, because that is
what is new -- the analysis functions underneath already have their own
tests.
"""
import os

os.environ["FRAMECHECK_API_MODE"] = "mock"
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

import threading  # noqa: E402
import time  # noqa: E402
from unittest import mock  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import jobs  # noqa: E402
from api import main as api_main  # noqa: E402


@pytest.fixture(autouse=True)
def clean_registry():
    jobs._jobs.clear()
    jobs._order.clear()
    jobs._running = None
    yield
    for job in list(jobs._jobs.values()):
        job._cancel.set()
    jobs._jobs.clear()
    jobs._order.clear()
    jobs._running = None


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestDetectorJob:
    def test_runs_every_detector_and_reports_counts(self):
        with mock.patch.object(jobs.psycopg2, "connect"), \
             mock.patch.object(jobs.detectors_module, "run_bfs_proximity", return_value=3), \
             mock.patch.object(jobs.detectors_module, "run_sybil_cluster", return_value=5), \
             mock.patch.object(jobs.detectors_module, "run_pagerank_threshold", return_value=7), \
             mock.patch.object(jobs.detectors_module, "run_impurity", return_value=1), \
             mock.patch.object(jobs.detectors_module, "run_engaged_proximity", return_value=0):
            job = jobs.start("detectors", snapshot_id=2, seed_wallet_id=2)
            assert wait_for(lambda: job.status == "done"), job.status
        assert job.result["flags_total"] == 16
        assert job.result["flags_per_detector"]["sybil_cluster"] == 5
        assert job.completed == job.total == 5

    def test_a_single_detector_runs_only_that_one(self):
        with mock.patch.object(jobs.psycopg2, "connect"), \
             mock.patch.object(jobs.detectors_module, "run_impurity", return_value=2) as impurity, \
             mock.patch.object(jobs.detectors_module, "run_bfs_proximity") as bfs:
            job = jobs.start("detectors", 2, 2, detector="impurity")
            assert wait_for(lambda: job.status == "done"), job.status
        assert impurity.call_count == 1
        assert bfs.call_count == 0


class TestAuditJob:
    def _conn_returning(self, flag_ids):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(f,) for f in flag_ids]
        return conn

    def test_audits_each_flag_and_tallies_verdicts(self):
        conn = self._conn_returning([1, 2, 3])
        verdicts = iter([{"verdict": "frameable"}, {"verdict": "robust"}, {"verdict": "frameable"}])
        with mock.patch.object(jobs.psycopg2, "connect", return_value=conn), \
             mock.patch.object(jobs.audit_module, "audit_flag", side_effect=lambda *a: next(verdicts)):
            job = jobs.start("audit", 2, 2)
            assert wait_for(lambda: job.status == "done"), job.status
        assert job.result == {"verdicts": {"frameable": 2, "robust": 1}, "audited": 3}

    def test_limit_samples_in_flag_id_order_like_the_cli(self):
        # A bounded run from the UI must pick the same flags as the same
        # bounded run from a terminal, or the numbers can't be compared.
        conn = self._conn_returning([1, 2])
        with mock.patch.object(jobs.psycopg2, "connect", return_value=conn), \
             mock.patch.object(jobs.audit_module, "audit_flag", return_value={"verdict": "robust"}):
            job = jobs.start("audit", 2, 2, limit=2)
            assert wait_for(lambda: job.status == "done"), job.status
        sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        assert "ORDER BY f.flag_id" in sql
        assert sql.index("ORDER BY f.flag_id") < sql.index("LIMIT")

    def test_progress_is_visible_while_it_runs(self):
        conn = self._conn_returning([1, 2, 3, 4, 5])
        gate = threading.Event()

        def slow_audit(*_args):
            gate.wait(timeout=5)
            return {"verdict": "robust"}

        with mock.patch.object(jobs.psycopg2, "connect", return_value=conn), \
             mock.patch.object(jobs.audit_module, "audit_flag", side_effect=slow_audit):
            job = jobs.start("audit", 2, 2)
            assert wait_for(lambda: job.status == "running" and job.total == 5)
            assert "auditing flag" in job.message
            gate.set()
            assert wait_for(lambda: job.status == "done"), job.status


class TestConcurrencyAndCancel:
    def test_a_second_job_is_refused_while_one_runs(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(1,)]
        gate = threading.Event()
        with mock.patch.object(jobs.psycopg2, "connect", return_value=conn), \
             mock.patch.object(jobs.audit_module, "audit_flag", side_effect=lambda *a: (gate.wait(5), {"verdict": "robust"})[1]):
            jobs.start("audit", 2, 2)
            assert wait_for(lambda: jobs.active_job() is not None)
            with pytest.raises(RuntimeError, match="already running"):
                jobs.start("detectors", 2, 2)
            gate.set()

    def test_cancel_stops_at_the_next_flag_rather_than_undoing_work(self):
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(i,) for i in range(50)]
        seen = []

        def audit(_conn, flag_id, _seed):
            seen.append(flag_id)
            time.sleep(0.01)
            return {"verdict": "robust"}

        with mock.patch.object(jobs.psycopg2, "connect", return_value=conn), \
             mock.patch.object(jobs.audit_module, "audit_flag", side_effect=audit):
            job = jobs.start("audit", 2, 2)
            assert wait_for(lambda: job.completed >= 1)
            assert jobs.cancel(job.job_id) is True
            assert wait_for(lambda: job.status == "cancelled"), job.status
        assert len(seen) < 50  # stopped early
        assert job.completed == len(seen)  # and reports what it actually did

    def test_cancelling_a_finished_job_is_refused(self):
        with mock.patch.object(jobs.psycopg2, "connect"), \
             mock.patch.object(jobs.detectors_module, "run_impurity", return_value=0):
            job = jobs.start("detectors", 2, 2, detector="impurity")
            assert wait_for(lambda: job.status == "done")
        assert jobs.cancel(job.job_id) is False

    def test_unknown_job_type_is_rejected_before_a_thread_starts(self):
        with pytest.raises(ValueError):
            jobs.start("delete_everything", 2, 2)
        assert jobs.active_job() is None


class TestFailure:
    def test_a_failed_job_reports_the_error_without_a_traceback(self):
        with mock.patch.object(jobs.psycopg2, "connect"), \
             mock.patch.object(jobs.detectors_module, "run_impurity",
                               side_effect=RuntimeError("relation \"flag\" does not exist")):
            job = jobs.start("detectors", 2, 2, detector="impurity")
            assert wait_for(lambda: job.status == "failed"), job.status
        assert job.error.startswith("RuntimeError:")
        assert "Traceback" not in job.error
        assert jobs.active_job() is None  # the lock is released


class TestJobEndpoints:
    def test_starting_a_job_in_mock_mode_is_a_clear_409(self):
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.post("/jobs", json={"job_type": "audit", "snapshot_id": 2, "seed_wallet_id": 2})
        assert resp.status_code == 409
        assert "live mode" in resp.json()["detail"]

    def test_unknown_job_id_is_404_and_says_why_it_might_be_missing(self):
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.get("/jobs/does-not-exist")
        assert resp.status_code == 404
        assert "restart" in resp.json()["detail"]

    def test_listing_jobs_is_newest_first(self):
        with mock.patch.object(jobs.psycopg2, "connect"), \
             mock.patch.object(jobs.detectors_module, "run_impurity", return_value=0):
            first = jobs.start("detectors", 2, 2, detector="impurity")
            assert wait_for(lambda: first.status == "done")
            second = jobs.start("detectors", 3, 2, detector="impurity")
            assert wait_for(lambda: second.status == "done")
        client = TestClient(api_main.app, raise_server_exceptions=False)
        rows = client.get("/jobs").json()
        assert [r["job_id"] for r in rows][:2] == [second.job_id, first.job_id]

    def test_the_registry_does_not_grow_without_bound(self):
        for i in range(jobs.MAX_REMEMBERED_JOBS + 10):
            jobs._remember(jobs.Job(job_id=f"j{i}", job_type="audit", snapshot_id=1, seed_wallet_id=1))
        assert len(jobs._jobs) == jobs.MAX_REMEMBERED_JOBS
