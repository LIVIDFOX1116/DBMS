"""The code paths moved onto the ORM, driven through a mocked Session --
offline, and deliberately not against SQLite: ON CONFLICT ... RETURNING,
JSONB, DISTINCT ON and the audit triggers are PostgreSQL-only, so a SQLite
pass would prove nothing. Statements are compiled with the PostgreSQL
dialect and checked for the SQL they will actually send.
"""
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.dialects import postgresql

from api import auth
from db.orm.models import AppUser

PG = postgresql.dialect()


def compiled(statement) -> str:
    return " ".join(str(statement.compile(dialect=PG)).split())


class TestAuthViaOrm:
    def _session_returning(self, **row):
        session = mock.MagicMock()
        row = {"role": "viewer", "username": None, "is_active": True, **row}
        session.execute.return_value.one.return_value = SimpleNamespace(**row)
        return session

    def test_upsert_returns_the_user_with_role_and_status(self):
        session = self._session_returning(user_id=7, google_sub="s", email="e@x", display_name="D", picture_url="p")
        user = auth.upsert_user(session, {"sub": "s", "email": "e@x", "name": "D", "picture": "p"})
        assert user == {"user_id": 7, "google_sub": "s", "email": "e@x", "display_name": "D", "picture_url": "p",
                        "role": "viewer", "username": None, "is_active": True}
        assert "password_hash" not in user

    def test_upsert_commits_before_returning(self):
        # The caller issues a session token for this row straight after.
        session = self._session_returning(user_id=1, google_sub="s", email="", display_name=None, picture_url=None)
        auth.upsert_user(session, {"sub": "s"})
        session.commit.assert_called_once()

    def test_upsert_statement_refreshes_profile_and_login_time_on_conflict(self):
        session = self._session_returning(user_id=1, google_sub="s", email="", display_name=None, picture_url=None)
        auth.upsert_user(session, {"sub": "s", "email": "e@x"})
        sql = compiled(session.execute.call_args.args[0])
        assert sql.startswith("INSERT INTO app_user")
        assert "ON CONFLICT (google_sub) DO UPDATE SET email = excluded.email" in sql
        assert "last_login_at = now()" in sql
        assert "created_at" not in sql, "created_at belongs to the first sign-in; the DDL default sets it"
        assert sql.endswith("RETURNING app_user.user_id, app_user.google_sub, app_user.email, "
                            "app_user.display_name, app_user.picture_url, "
                            "app_user.role, app_user.username, app_user.is_active")
        # The role is set on INSERT only: a later sign-in never overwrites
        # a role an admin changed.
        assert "role = excluded.role" not in sql and "role" not in sql.split("DO UPDATE SET")[1].split("RETURNING")[0]

    def test_upsert_binds_values_rather_than_interpolating_them(self):
        session = self._session_returning(user_id=1, google_sub="x", email="", display_name=None, picture_url=None)
        hostile = "'); DROP TABLE app_user; --"
        auth.upsert_user(session, {"sub": hostile, "email": hostile})
        statement = session.execute.call_args.args[0]
        assert hostile not in compiled(statement)
        assert hostile in statement.compile(dialect=PG).params.values()

    def test_fetch_is_a_primary_key_lookup(self):
        session = mock.MagicMock()
        session.get.return_value = AppUser(user_id=3, google_sub="s", email="e", display_name=None, picture_url=None,
                                           role="analyst", username="u", is_active=True, password_hash="$2b$12$x")
        assert auth.pg_user_fetch(session, 3) == {
            "user_id": 3, "google_sub": "s", "email": "e", "display_name": None, "picture_url": None,
            "role": "analyst", "username": "u", "is_active": True}
        session.get.assert_called_once_with(AppUser, 3)

    def test_fetch_of_a_missing_user_is_none(self):
        session = mock.MagicMock()
        session.get.return_value = None
        assert auth.pg_user_fetch(session, 404) is None


# ---------------------------------------------------------------------------
# Flag / audit / stats endpoints
# ---------------------------------------------------------------------------

import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

os.environ.setdefault("FRAMECHECK_AUTH_MODE", "disabled")

from fastapi.testclient import TestClient  # noqa: E402

import api.main as api_main  # noqa: E402


class _Recorder:
    """A session that records every statement and answers with canned rows."""

    def __init__(self, scalar=None, rows=(), one=None):
        self.statements, self._scalar, self._rows, self._one = [], scalar, list(rows), one

    def _result(self):
        result = mock.MagicMock()
        result.__iter__.return_value = iter(self._rows)
        result.first.return_value = None
        result.one.return_value = self._one
        result.all.return_value = []
        result.keys.return_value = []
        return result

    def execute(self, statement, *a):
        self.statements.append(statement)
        return self._result()

    def scalar(self, statement, *a):
        self.statements.append(statement)
        return self._scalar

    def scalars(self, statement, *a):
        self.statements.append(statement)
        return iter([31, 32])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _live(monkeypatch, recorder):
    monkeypatch.setattr(api_main, "using_mock", lambda: False)
    monkeypatch.setattr(api_main, "get_session", lambda: recorder)
    return TestClient(api_main.app)


class TestLatestRunIds:
    def test_same_rule_as_the_raw_helper(self):
        # resolve_latest_run_ids (raw, kept for the raw endpoints) and
        # latest_run_ids (ORM) must pick the same runs: newest DONE run per
        # detector on the snapshot.
        recorder = _Recorder()
        assert api_main.latest_run_ids(recorder, 4, ("bfs_proximity", "sybil_cluster")) == [31, 32]
        sql = compiled(recorder.statements[0])
        assert sql.startswith("SELECT DISTINCT ON (algorithm.algorithm_name) analysis_run.run_id")
        assert "analysis_run.status = %(status_1)s" in sql
        assert sql.endswith("ORDER BY algorithm.algorithm_name, analysis_run.run_id DESC")
        params = recorder.statements[0].compile(dialect=PG).params
        assert params["status_1"] == "DONE" and params["snapshot_id_1"] == 4

    def test_run_ids_bind_as_one_array_parameter(self):
        from db.orm.models import Flag
        clause = api_main.in_run_ids(Flag.run_id, [29, 30])
        # One parameter, typed as an array, whatever the number of runs.
        assert " ".join(str(clause.compile(dialect=PG)).split()) == "flag.run_id = ANY (%(param_1)s::BIGINT[])"
        assert clause.compile(dialect=PG).params == {"param_1": [29, 30]}

class TestLatestAuditView:
    """The rule the three audit endpoints rely on, now in the database:
    v_flag_latest_audit (migration 011)."""
    sql = " ".join((pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
                    / "011_view_procedures_alert_status.sql").read_text(encoding="utf-8").split())

    def test_newest_audit_per_flag_with_id_as_tiebreak(self):
        view = self.sql[self.sql.index("CREATE OR REPLACE VIEW v_flag_latest_audit"):]
        view = view[:view.index(";")]
        assert "SELECT DISTINCT ON (fa.flag_id)" in view
        assert view.endswith("ORDER BY fa.flag_id, fa.created_at DESC, fa.flag_audit_id DESC")

    def test_view_model_columns_match_the_view(self):
        from db.orm.models import FlagLatestAudit
        view = self.sql[self.sql.index("CREATE OR REPLACE VIEW v_flag_latest_audit"):]
        select_list = view[view.index("DISTINCT ON (fa.flag_id)") + len("DISTINCT ON (fa.flag_id)"):view.index(" FROM ")]
        view_columns = [c.strip().split(".")[-1] for c in select_list.split(",")]
        assert [c.name for c in FlagLatestAudit.__table__.columns] == view_columns

    def test_the_view_is_not_in_the_table_metadata(self):
        from db.orm.models import Base
        assert "v_flag_latest_audit" not in Base.metadata.tables


class TestAggregates:
    def test_audit_overview_median_is_percentile_cont_over_frameable_costs(self, monkeypatch):
        recorder = _Recorder(scalar=None, rows=[])
        body = _live(monkeypatch, recorder).get("/audit/overview").json()
        assert body["framing_cost_median_wei"] is None and body["total_flags"] == 0
        per_detector, median = (compiled(s) for s in recorder.statements)
        assert re.search(r"count\(\*\) FILTER \(WHERE v_flag_latest_audit\.verdict = %\(verdict_1\)s(::VARCHAR)?\) "
                         r"AS frameable_count", per_detector)
        assert "FROM flag LEFT OUTER JOIN v_flag_latest_audit" in per_detector, "unaudited flags still count as flags"
        assert re.search(r"percentile_cont\(%\(percentile_cont_\d\)s\) WITHIN GROUP "
                         r"\(ORDER BY v_flag_latest_audit\.framing_cost_wei\)", median)
        assert "v_flag_latest_audit.framing_cost_wei IS NOT NULL" in median

    def test_audit_overview_with_snapshot_scopes_both_queries(self, monkeypatch):
        recorder = _Recorder(scalar=None, rows=[])
        _live(monkeypatch, recorder).get("/audit/overview", params={"snapshot_id": 1})
        dedup, per_detector, median = (compiled(s) for s in recorder.statements)
        assert "DISTINCT ON (algorithm.algorithm_name)" in dedup
        assert "flag.run_id = ANY" in per_detector and "v_flag_latest_audit.run_id = ANY" in median

    def test_stats_overview_risk_tiers_use_filtered_counts(self, monkeypatch):
        from types import SimpleNamespace
        recorder = _Recorder(scalar=5, rows=[("HIGH", 2), ("BOGUS", 9)],
                             one=SimpleNamespace(unknown=1, high=2, med=3, low=4))
        body = _live(monkeypatch, recorder).get("/stats/overview").json()
        assert body["wallet_count"] == 5
        assert body["alert_severity_counts"] == {"HIGH": 2, "MEDIUM": 0, "LOW": 0}
        assert body["wallet_risk_tier_counts"] == {"low": 4, "med": 3, "high": 2, "unknown": 1}
        tiers = compiled(recorder.statements[-1])
        assert "count(*) FILTER (WHERE wallet.risk_score IS NULL) AS unknown" in tiers
        assert re.search(r"count\(\*\) FILTER \(WHERE wallet\.risk_score >= %\(risk_score_\d\)s(::INTEGER)? "
                         r"AND wallet\.risk_score < %\(risk_score_\d\)s(::INTEGER)?\) AS med", tiers)


class TestFlagDetailStatements:
    def test_evidence_edges_take_the_signer_from_the_transaction(self, monkeypatch):
        # CLAUDE.md: the signer is transaction.from_wallet_id, never
        # token_transfer.from_wallet_id.
        recorder = _Recorder()
        _live(monkeypatch, recorder).get("/flags/1/evidence-edges")
        sql = compiled(recorder.statements[0])
        assert "token_transfer" not in sql
        assert "JOIN wallet AS wallet_1 ON wallet_1.wallet_id = transaction.from_wallet_id" in sql
        assert "LEFT OUTER JOIN wallet AS wallet_2 ON wallet_2.wallet_id = transaction.to_wallet_id" in sql
        assert "CAST(transaction.value_wei AS TEXT) AS value_wei" in sql

    def test_audit_csv_joins_the_newest_audit_laterally(self, monkeypatch):
        recorder = _Recorder()
        resp = _live(monkeypatch, recorder).get("/flags/1/audit.csv")
        assert resp.status_code == 404      # the recorder has no such flag
        sql = compiled(recorder.statements[0])
        assert "LEFT OUTER JOIN LATERAL (SELECT" in sql
        assert "WHERE flag_audit.flag_id = flag.flag_id ORDER BY flag_audit.created_at DESC, flag_audit.flag_audit_id DESC" in sql

    def test_unaudited_flag_is_404_not_an_empty_200(self, monkeypatch):
        assert _live(monkeypatch, _Recorder()).get("/flags/11/audit").status_code == 404


# ---------------------------------------------------------------------------
# graph/audit.py persist_audit
# ---------------------------------------------------------------------------

class TestPersistAudit:
    def _run(self, **kwargs):
        from graph.audit import persist_audit
        conn = mock.MagicMock()
        persist_audit(conn, 7, "frameable", framing_cost_wei=10**18, framing_gas=21000,
                      minimal_tx_set=["0xab"], method="exact", backend="cpu", runtime_ms=5, **kwargs)
        sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        return conn, " ".join(sql.split()), params

    def test_insert_is_built_from_the_model_and_never_names_the_hashes(self):
        _, sql, _ = self._run()
        assert sql.startswith("INSERT INTO flag_audit (flag_id, verdict, framing_cost_wei, framing_gas, "
                              "minimal_tx_set, method, backend, runtime_ms) VALUES")
        assert "prev_hash" not in sql and "row_hash" not in sql, "the chain trigger computes these"
        assert "UPDATE" not in sql

    def test_values_are_bound_and_json_serialised(self):
        _, sql, params = self._run()
        assert "%(flag_id)s" in sql and "0xab" not in sql
        assert params["minimal_tx_set"] == '["0xab"]'
        assert params["framing_cost_wei"] == 10**18

    def test_commit_true_commits(self):
        conn, _, _ = self._run()
        conn.commit.assert_called_once()

    def test_commit_false_leaves_the_transaction_to_the_caller(self):
        # benchmark_audit.py / run_experiment.py roll back what they wrote.
        conn, _, _ = self._run(commit=False)
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# /cluster-membership and the audit job's target query
# ---------------------------------------------------------------------------

class TestRemainingConversions:
    def test_cluster_membership_aggregates_in_cluster_order(self, monkeypatch):
        recorder = _Recorder(rows=[(5, [1, 2])])
        body = _live(monkeypatch, recorder).get("/cluster-membership?cluster_ids=2,1").json()
        assert body == {"5": [1, 2]}
        sql = compiled(recorder.statements[0])
        assert "array_agg(cluster_member.cluster_id ORDER BY cluster_member.cluster_id)" in sql
        assert "cluster_member.cluster_id = ANY (%(param_1)s::BIGINT[])" in sql
        assert sql.endswith("GROUP BY cluster_member.wallet_id")

    def test_audit_job_targets_are_scoped_through_the_runs_snapshot(self):
        from api import jobs

        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        job = jobs.Job(job_id="t", job_type="audit", snapshot_id=4, seed_wallet_id=2, detector="bfs_proximity", limit=5)
        jobs._run_audit(job, conn)
        sql, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        sql = " ".join(sql.split())
        assert "FROM flag AS f JOIN analysis_run ON analysis_run.run_id = f.run_id" in sql
        assert "analysis_run.snapshot_id = %(snapshot_id_1)s" in sql and params["snapshot_id_1"] == 4
        assert "f.detector = %(detector_1)s" in sql and params["detector_1"] == "bfs_proximity"
        assert sql.endswith("ORDER BY f.flag_id LIMIT %(param_1)s") and params["param_1"] == 5
        assert job.result == {"verdicts": {}, "audited": 0}
