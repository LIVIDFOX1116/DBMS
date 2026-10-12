"""
Offline tests for Feature 2: the audit trail the database enforces
(db/migrations/009_framecheck_audit_trail.sql).

No Postgres here, so these cover what can be checked without one: that the
migration says the things it must say, that the endpoints behave in demo
mode, and that the timestamp handling is right. The behaviour that only a
server can demonstrate -- a refused UPDATE, a broken chain, a point-in-time
query -- is verified against a live database and recorded in docs/STABILITY.md.
"""
import os
import pathlib
import re
import sys

os.environ["FRAMECHECK_API_MODE"] = "mock"
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app, normalize_timestamp_arg  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

MIGRATION = (pathlib.Path(__file__).parent.parent / "db" / "migrations"
             / "009_framecheck_audit_trail.sql").read_text(encoding="utf-8")


class TestMigrationShape:
    """The guarantees are in the SQL, so the SQL is what gets asserted."""

    def test_update_and_delete_are_both_guarded(self):
        assert re.search(r"CREATE TRIGGER flag_audit_append_only\s+BEFORE UPDATE OR DELETE ON flag_audit",
                         MIGRATION), "the guard must cover both UPDATE and DELETE"

    def test_an_update_is_refused_outright_with_no_escape_hatch(self):
        """The door is for deletes only. There is no legitimate update: a
        corrected verdict is a new row."""
        guard = MIGRATION[MIGRATION.index("FUNCTION flag_audit_guard()"):]
        guard = guard[:guard.index("DROP TRIGGER IF EXISTS flag_audit_append_only")]
        update_branch = guard[guard.index("IF TG_OP = 'UPDATE'"):guard.index("IF coalesce(current_setting")]
        assert "RAISE EXCEPTION" in update_branch
        assert "allow_audit_delete" not in update_branch

    def test_a_permitted_delete_is_still_recorded(self):
        """A gap in the chain with no tombstone is tampering; a gap with one
        is housekeeping. So the tombstone insert must not sit inside the
        branch that refuses the delete."""
        guard = MIGRATION[MIGRATION.index("FUNCTION flag_audit_guard()"):]
        guard = guard[:guard.index("DROP TRIGGER IF EXISTS flag_audit_append_only")]
        assert "INSERT INTO flag_audit_tombstone" in guard
        assert guard.index("INSERT INTO flag_audit_tombstone") > guard.index("allow_audit_delete")
        assert "current_user" in guard, "who deleted it is part of the record"
        assert "audit_delete_reason" in guard, "and so is why"

    def test_the_hash_covers_the_verdict_and_its_position(self):
        payload = MIGRATION[MIGRATION.index("FUNCTION flag_audit_payload"):]
        payload = payload[:payload.index("$$;")]
        for column in ("flag_audit_id", "flag_id", "verdict", "framing_cost_wei",
                       "framing_gas", "minimal_tx_set", "method", "backend",
                       "runtime_ms", "created_at"):
            assert column in payload, f"{column} is not covered by the hash"
        # Including the id is what stops a row being renumbered or reordered.
        assert "a.flag_audit_id" in payload

    def test_the_hash_does_not_cover_itself(self):
        payload = MIGRATION[MIGRATION.index("FUNCTION flag_audit_payload"):]
        payload = payload[:payload.index("$$;")]
        assert "a.row_hash" not in payload and "a.prev_hash" not in payload

    def test_one_serialization_serves_both_writer_and_verifier(self):
        """Two serializations would drift, and the chain would start failing
        on rows nobody had touched."""
        assert MIGRATION.count("CREATE OR REPLACE FUNCTION flag_audit_payload") == 1
        link = MIGRATION[MIGRATION.index("FUNCTION flag_audit_chain_link"):]
        link = link[:link.index("END $$;")]
        verify = MIGRATION[MIGRATION.index("FUNCTION flag_audit_chain_verify"):]
        assert "flag_audit_payload(NEW)" in link
        assert "flag_audit_payload(r)" in verify

    def test_concurrent_appends_are_serialized(self):
        """Two inserts reading the same tail row would fork the chain: two
        rows claiming the same predecessor."""
        link = MIGRATION[MIGRATION.index("FUNCTION flag_audit_chain_link"):]
        link = link[:link.index("END $$;")]
        assert "pg_advisory_xact_lock" in link

    def test_existing_rows_are_backfilled_before_the_guard_exists(self):
        """The backfill is an UPDATE, and the guard is about to make those
        impossible. Order is the whole trick."""
        assert MIGRATION.index("WHERE row_hash IS NULL") < \
               MIGRATION.index("CREATE TRIGGER flag_audit_append_only")

    def test_history_is_not_a_foreign_key_to_flag(self):
        """History has to outlive the row it describes, and
        scripts/run_experiment.py deletes flags wholesale."""
        history = MIGRATION[MIGRATION.index("CREATE TABLE IF NOT EXISTS flag_history"):]
        history = history[:history.index(");")]
        assert "REFERENCES flag" not in history

    def test_versions_cannot_overlap_whichever_path_is_taken(self):
        """EXCLUDE needs btree_gist, which is contrib and not guaranteed on a
        user's machine now that this schema ships inside a desktop app."""
        assert "EXCLUDE USING gist (flag_id WITH =, sys_period WITH &&)" in MIGRATION
        assert "flag_history_one_version_per_start" in MIGRATION, "no fallback for a server without btree_gist"
        assert "btree_gist unavailable" in MIGRATION, "the fallback should say why it happened"

    def test_a_version_period_cannot_be_empty(self):
        assert "CHECK (NOT isempty(sys_period))" in MIGRATION
        capture = MIGRATION[MIGRATION.index("FUNCTION flag_version_capture"):]
        capture = capture[:capture.index("END $$;")]
        assert "clock_timestamp()" in capture, "now() is identical twice in one transaction"
        assert "1 microsecond" in capture

    def test_the_migration_is_idempotent_in_its_own_text(self):
        """Every migration here can be re-run; verified live too, but the
        obvious cases are cheap to assert."""
        assert MIGRATION.count("ADD COLUMN IF NOT EXISTS") >= 2
        assert MIGRATION.count("CREATE TABLE IF NOT EXISTS") >= 2
        assert MIGRATION.count("CREATE OR REPLACE FUNCTION") >= 6
        for trigger in ("flag_audit_chain", "flag_audit_append_only", "flag_versioning"):
            assert f"DROP TRIGGER IF EXISTS {trigger}" in MIGRATION

    def test_time_travel_excludes_rows_that_did_not_exist_yet(self):
        as_of = MIGRATION[MIGRATION.index("FUNCTION flag_as_of"):]
        as_of = as_of[:as_of.index("$$;")]
        assert "f.created_at::timestamptz <= at_time" in as_of
        assert "sys_period @> at_time" in as_of


class TestToolingGoesThroughTheDoor:
    """The two scripts that legitimately remove audit rows have to announce
    it, or they break the moment the migration is applied."""

    def test_the_benchmark_announces_and_explains_its_deletes(self):
        src = (pathlib.Path(__file__).parent.parent / "scripts" / "benchmark_audit.py").read_text(encoding="utf-8")
        cleanup = src[src.index("def cleanup_benchmark_audits"):]
        cleanup = cleanup[:cleanup.index("\ndef ")]
        assert "framecheck.allow_audit_delete" in cleanup
        assert "framecheck.audit_delete_reason" in cleanup
        assert cleanup.index("allow_audit_delete") < cleanup.index("DELETE FROM flag_audit")

    def test_the_experiment_runner_does_too(self):
        src = (pathlib.Path(__file__).parent.parent / "scripts" / "run_experiment.py").read_text(encoding="utf-8")
        assert "framecheck.allow_audit_delete" in src
        assert src.index("allow_audit_delete") < src.index("DELETE FROM flag_audit")


class TestTimestampArgument:
    """A caller who passes a perfectly good ISO timestamp with a +00:00
    offset arrives here with a space instead of the '+', because that is what
    '+' means in a query string. Found by passing a real flag_history
    timestamp straight into the endpoint."""

    def test_a_lost_plus_is_read_back(self):
        assert normalize_timestamp_arg("2026-10-12T01:35:55 00:00") == "2026-10-12T01:35:55+00:00"
        assert normalize_timestamp_arg("2026-10-12T01:35:55.123456 00:00") == \
            "2026-10-12T01:35:55.123456+00:00"
        assert normalize_timestamp_arg("2026-10-12T01:35:55 0530") == "2026-10-12T01:35:55+0530"

    def test_everything_else_is_left_exactly_as_given(self):
        for value in ("2026-10-12T01:35:55Z", "2026-10-12", "not-a-date",
                      "2026-10-12 01:35:55", "now()"):
            assert normalize_timestamp_arg(value) == value

    def test_a_space_separated_date_and_time_is_not_mistaken_for_an_offset(self):
        """'2026-10-12 01:35:55' is a valid timestamp, not a date with an
        offset -- mangling it into '2026-10-12+01:35:55' would turn a working
        call into a 422."""
        assert "+" not in normalize_timestamp_arg("2026-10-12 01:35:55")


class TestEndpointsInDemoMode:
    def test_the_trail_endpoint_answers_without_a_database(self):
        body = client.get("/audit/trail").json()
        assert body["available"] is True
        assert "intact" in body and "tombstones" in body

    def test_versions_answers_for_an_unknown_flag_without_a_500(self):
        body = client.get("/flags/999999/versions").json()
        assert body["available"] is True
        assert body["versions"] == []

    def test_as_of_is_not_shadowed_by_the_flag_id_routes(self):
        """/flags/as-of has to stay reachable: the project has been bitten by
        a two-segment route disappearing behind a parameterised one before."""
        res = client.get("/flags/as-of?at=2026-10-11T00:00:00Z")
        assert res.status_code == 200
        assert "flags" in res.json()

    def test_as_of_caps_its_page_size(self):
        body = client.get("/flags/as-of?at=2026-10-11T00:00:00Z&limit=100000").json()
        assert len(body["flags"]) <= 500
