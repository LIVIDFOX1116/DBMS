"""
Offline tests for api/dbsetup.py and the /db/* routes.

No live Postgres: the pure helpers are called directly, and the operations
that need a connection are driven through a fake cursor that records the SQL
it was handed. That is enough to pin the things that actually matter here --
that a table name from the UI can never become SQL, that a database whose
migration history is unknown is refused rather than guessed at, and that
nothing in this module drops or truncates anything.
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
# The /db/* routes are session-gated like every other non-public route --
# privileged setup operations should be, and the desktop app reaches them
# because it runs with the gate off. Same opt-out the other API tests use.
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

import psycopg2
from fastapi.testclient import TestClient

from api import dbsetup, main


class FakeCursor:
    """Replays a queue of answers and records every statement. `execute`
    takes psycopg2.sql.Composed objects too, which is what the real code
    builds for identifiers -- they are stringified for the record."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.executed = []
        self.description = None

    def execute(self, statement, params=None):
        self.executed.append((repr(statement), params))
        if self.answers:
            head = self.answers.pop(0)
            self._result = head.get("fetchone"), head.get("fetchall"), head.get("description")
        else:
            self._result = None, [], None
        self.description = self._result[2]

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result[1] or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.committed += 1
        return False

    def close(self):
        pass


class TestRedaction(unittest.TestCase):
    def test_the_password_never_leaves_the_module(self):
        out = dbsetup.redact({"host": "h", "port": 5432, "dbname": "d", "user": "u", "password": "hunter2"})
        self.assertNotIn("password", out)
        self.assertTrue(out["password_set"])
        self.assertNotIn("hunter2", str(out))

    def test_no_password_is_distinguishable_from_a_set_one(self):
        self.assertFalse(dbsetup.redact({"password": ""})["password_set"])


class TestNormaliseDsn(unittest.TestCase):
    def test_trims_and_defaults_the_port(self):
        dsn = dbsetup.normalise_dsn({"host": " localhost ", "dbname": "framecheck", "user": "postgres"})
        self.assertEqual(dsn["host"], "localhost")
        self.assertEqual(dsn["port"], "5432")

    def test_a_half_filled_form_fails_here_not_inside_libpq(self):
        with self.assertRaises(ValueError) as ctx:
            dbsetup.normalise_dsn({"host": "localhost", "user": ""})
        self.assertIn("dbname", str(ctx.exception))
        self.assertIn("user", str(ctx.exception))

    def test_a_nonsense_port_is_rejected(self):
        for port in ("0", "70000", "abc", "-1"):
            with self.assertRaises(ValueError):
                dbsetup.normalise_dsn({"host": "h", "dbname": "d", "user": "u", "port": port})

    def test_unknown_fields_are_dropped_rather_than_passed_to_psycopg2(self):
        dsn = dbsetup.normalise_dsn({"host": "h", "dbname": "d", "user": "u",
                                     "options": "-c search_path=evil", "sslmode": "disable"})
        self.assertEqual(set(dsn), set(dbsetup.DSN_FIELDS))


class TestMigrationOrdering(unittest.TestCase):
    def test_numeric_prefixes_sort_numerically_not_lexically(self):
        names = [p.name for p in dbsetup.migration_files()]
        self.assertEqual(names, sorted(names, key=lambda n: int(n.split("_")[0])))

    def test_pending_is_available_minus_applied_in_order(self):
        available = ["001_a.sql", "002_b.sql", "003_c.sql"]
        self.assertEqual(dbsetup.pending_migrations(available, {"002_b.sql"}),
                         ["001_a.sql", "003_c.sql"])

    def test_every_repo_migration_is_discovered(self):
        found = {p.name for p in dbsetup.migration_files()}
        on_disk = {p.name for p in (dbsetup.MIGRATIONS_DIR).glob("*.sql")}
        self.assertEqual(found, on_disk)
        self.assertTrue(found, "the repo has migrations; discovering none would mean a broken path")


class TestIdentifierSafety(unittest.TestCase):
    def test_sql_injection_shapes_are_rejected(self):
        for bad in ("wallet; DROP TABLE wallet", "wallet'", 'wallet"', "wallet--", "1wallet",
                    "", None, "a" * 64, "pg_catalog.pg_tables"):
            self.assertFalse(dbsetup.is_safe_identifier(bad), f"{bad!r} should be rejected")

    def test_real_table_names_are_accepted(self):
        for good in ("wallet", "token_transfer", "flag_audit", "_x1"):
            self.assertTrue(dbsetup.is_safe_identifier(good))

    def test_sample_table_rejects_a_bad_name_before_touching_the_database(self):
        cur = FakeCursor()
        with self.assertRaises(ValueError):
            dbsetup.sample_table(FakeConn(cur), "wallet; DROP TABLE wallet")
        self.assertEqual(cur.executed, [], "nothing should reach the database")

    def test_sample_table_rejects_a_name_absent_from_the_catalogue(self):
        cur = FakeCursor([{"fetchone": None}])          # the catalogue lookup finds nothing
        with self.assertRaises(LookupError):
            dbsetup.sample_table(FakeConn(cur), "not_a_table")
        self.assertEqual(len(cur.executed), 1, "it must stop after the catalogue check")

    def test_sample_table_quotes_the_identifier_and_parameterises_the_limit(self):
        cur = FakeCursor([
            {"fetchone": (1,)},
            {"fetchall": [(1, "0xabc")], "description": [("wallet_id",), ("address",)]},
        ])
        out = dbsetup.sample_table(FakeConn(cur), "wallet", limit=10)
        self.assertEqual(out["columns"], ["wallet_id", "address"])
        statement, params = cur.executed[1]
        self.assertIn("Identifier('wallet')", statement)
        self.assertEqual(params, (10,))

    def test_sample_limit_is_clamped(self):
        for asked, expected in ((0, 1), (-5, 1), (10_000, 500)):
            cur = FakeCursor([{"fetchone": (1,)}, {"fetchall": [], "description": []}])
            out = dbsetup.sample_table(FakeConn(cur), "wallet", limit=asked)
            self.assertEqual(out["limit"], expected)


class TestTableInventory(unittest.TestCase):
    """Counting every row of all 23 tables on a real database measured
    6.85s, so the panel uses reltuples. The -1 case matters: Postgres
    reports it on a never-analysed table and showing "-1 rows" would be
    worse than showing nothing."""

    def test_estimates_come_from_one_query(self):
        cur = FakeCursor([{"fetchall": [("wallet", 610055.0, 123456), ("flag", 128637.0, 7890)]}])
        inv = dbsetup.table_inventory(FakeConn(cur))
        self.assertEqual(len(cur.executed), 1)
        self.assertEqual(inv[0], {"table": "wallet", "rows": 610055, "rows_are_estimates": True,
                                  "estimate_unavailable": False, "bytes": 123456})

    def test_a_never_analysed_table_reports_unknown_not_minus_one(self):
        cur = FakeCursor([{"fetchall": [("fresh", -1.0, 8192)]}])
        row = dbsetup.table_inventory(FakeConn(cur))[0]
        self.assertIsNone(row["rows"])
        self.assertTrue(row["estimate_unavailable"])

    def test_exact_count_validates_the_name_before_querying(self):
        cur = FakeCursor()
        with self.assertRaises(ValueError):
            dbsetup.exact_row_count(FakeConn(cur), "wallet; DROP TABLE wallet")
        self.assertEqual(cur.executed, [])

    def test_exact_count_is_marked_as_not_an_estimate(self):
        cur = FakeCursor([{"fetchone": (1,)}, {"fetchone": (610055,)}])
        out = dbsetup.exact_row_count(FakeConn(cur), "wallet")
        self.assertEqual(out["rows"], 610055)
        self.assertFalse(out["rows_are_estimates"])


class TestJsonable(unittest.TestCase):
    def test_wei_decimals_become_strings_not_floats(self):
        import decimal
        wei = decimal.Decimal("1234567890123456789012345678")
        self.assertEqual(dbsetup._jsonable(wei), str(wei))

    def test_dates_and_bytes_are_encodable(self):
        import datetime
        self.assertEqual(dbsetup._jsonable(datetime.date(2026, 10, 11)), "2026-10-11")
        self.assertEqual(dbsetup._jsonable(b"\xde\xad"), "dead")


class TestSchemaState(unittest.TestCase):
    def test_no_framecheck_tables_reads_as_empty(self):
        cur = FakeCursor([{"fetchone": (False,)}, {"fetchone": (False,)}, {"fetchone": (0,)}])
        state = dbsetup.schema_state(FakeConn(cur))
        self.assertEqual(state["state"], "empty")
        self.assertEqual(state["migrations_pending"], state["migrations_available"])

    def test_tables_without_tracking_reads_as_untracked_not_current(self):
        """The developer databases that predate this feature are here, and
        assuming they are current is exactly the wrong guess."""
        cur = FakeCursor([{"fetchone": (True,)}, {"fetchone": (False,)}, {"fetchone": (18,)}])
        state = dbsetup.schema_state(FakeConn(cur))
        self.assertEqual(state["state"], "untracked")
        self.assertIsNone(state["migrations_pending"], "unknown must not be reported as a list")

    def test_apply_schema_refuses_an_untracked_database(self):
        cur = FakeCursor([{"fetchone": (True,)}, {"fetchone": (False,)}, {"fetchone": (18,)}])
        with self.assertRaises(RuntimeError) as ctx:
            dbsetup.apply_schema(FakeConn(cur))
        self.assertIn("unknown", str(ctx.exception))

    def test_adopt_refuses_a_database_with_no_framecheck_tables(self):
        cur = FakeCursor([{"fetchone": (False,)}, {"fetchone": (False,)}, {"fetchone": (0,)}])
        with self.assertRaises(RuntimeError):
            dbsetup.mark_existing_as_applied(FakeConn(cur))


class TestNothingDestructive(unittest.TestCase):
    def test_the_module_contains_no_drop_truncate_or_unscoped_delete(self):
        """A standing repo rule, worth a test rather than a comment: this is
        the one module whose whole job is schema manipulation."""
        source = pathlib.Path(dbsetup.__file__).read_text(encoding="utf-8")
        # Uppercase only: the module's own prose explains that it does not
        # drop or truncate anything, and that sentence is not a statement.
        for forbidden in ("DROP TABLE", "DROP DATABASE", "DROP SCHEMA", "TRUNCATE", "DELETE FROM"):
            self.assertNotIn(forbidden, source)


class TestDbRoutes(unittest.TestCase):
    """The routes, with dbsetup's operations stubbed: what is pinned here is
    status-code mapping and that nothing leaks a password."""

    def setUp(self):
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(dbsetup.clear_runtime_dsn)

    def test_internal_db_settings_needs_the_internal_token(self):
        body = {"host": "localhost", "dbname": "d", "user": "u", "password": "p"}
        res = self.client.post("/internal/db-settings", json=body)
        self.assertEqual(res.status_code, 403)
        self.assertFalse(dbsetup.has_runtime_dsn(), "a rejected call must not change the setting")

    def test_internal_db_settings_accepts_the_right_token_and_hides_the_password(self):
        main._runtime_state["internal_token"] = "tok"
        self.addCleanup(main._runtime_state.__setitem__, "internal_token", "")
        with mock.patch.object(main, "close_pool") as closed:
            res = self.client.post("/internal/db-settings",
                                   json={"host": "localhost", "dbname": "d", "user": "u", "password": "secret"},
                                   headers={"x-internal-token": "tok"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("secret", res.text)
        self.assertTrue(res.json()["settings"]["password_set"])
        closed.assert_called_once()

    def test_db_settings_reports_where_the_connection_came_from(self):
        res = self.client.get("/db/settings")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["source"], "environment")
        self.assertNotIn("password\"", res.text)

    def test_db_status_reports_an_unreachable_server_as_data_not_an_error(self):
        with mock.patch.object(dbsetup, "server_probe",
                               return_value={"reachable": False, "error": "connection refused",
                                             "database_exists": None, "server_version": None}):
            res = self.client.get("/db/status")
        self.assertEqual(res.status_code, 200, "the setup view must still render")
        self.assertFalse(res.json()["server"]["reachable"])
        self.assertIsNone(res.json()["schema"])

    def test_switching_to_live_is_refused_while_the_database_is_unreachable(self):
        with mock.patch.object(dbsetup, "server_probe",
                               return_value={"reachable": False, "error": "connection refused",
                                             "database_exists": None, "server_version": None}):
            res = self.client.post("/db/mode", json={"mode": "live"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("connection refused", res.json()["detail"])
        self.assertTrue(main.using_mock(), "a refused switch must leave the mode alone")

    def test_switching_to_live_is_refused_before_the_schema_is_applied(self):
        with mock.patch.object(dbsetup, "server_probe",
                               return_value={"reachable": True, "error": None,
                                             "database_exists": True, "server_version": "PostgreSQL 16"}), \
             mock.patch.object(dbsetup, "connect"), \
             mock.patch.object(dbsetup, "schema_state", return_value={"schema_applied": False}):
            res = self.client.post("/db/mode", json={"mode": "live"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("schema", res.json()["detail"])

    def test_an_invalid_mode_is_a_422(self):
        self.assertEqual(self.client.post("/db/mode", json={"mode": "sideways"}).status_code, 422)

    def test_mode_can_be_switched_back_to_demo_without_a_database(self):
        res = self.client.post("/db/mode", json={"mode": "mock"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["mode"], "mock")
        self.assertTrue(main.using_mock())

    def test_an_unreachable_database_is_a_503_not_a_500(self):
        with mock.patch.object(dbsetup, "connect", side_effect=psycopg2.OperationalError("no server")):
            for path in ("/db/tables", "/db/tables/wallet", "/db/schema", "/db/adopt"):
                res = self.client.post(path) if path in ("/db/schema", "/db/adopt") else self.client.get(path)
                self.assertEqual(res.status_code, 503, path)

    def test_browsing_an_unknown_table_is_a_404(self):
        with mock.patch.object(dbsetup, "connect"), \
             mock.patch.object(dbsetup, "sample_table", side_effect=LookupError("no such table")):
            res = self.client.get("/db/tables/nope")
        self.assertEqual(res.status_code, 404)

    def test_create_reports_an_existing_database_rather_than_recreating_it(self):
        with mock.patch.object(dbsetup, "create_database",
                               return_value={"created": False, "detail": "already exists -- left untouched"}):
            res = self.client.post("/db/create")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["created"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLoopbackFallback:
    """On Windows, "localhost" and 127.0.0.1 are not the same address, and
    Postgres running in WSL listens on the VM's IPv4 loopback only. Measured
    on this machine: 127.0.0.1:5432 accepted every connection while
    ::1:5432 refused every one -- and "localhost" resolves to ::1 first. A
    settings panel that just says "unreachable" makes that miserable to
    debug, so connect() retries the IPv4 address.
    """

    def test_a_loopback_name_is_retried_as_ipv4(self, monkeypatch):
        import psycopg2

        from api import dbsetup

        attempts = []

        def fake_connect(**params):
            attempts.append(params["host"])
            if params["host"] != "127.0.0.1":
                raise psycopg2.OperationalError("connection refused (::1)")
            return object()

        monkeypatch.setattr(dbsetup.psycopg2, "connect", fake_connect)
        conn = dbsetup.connect({"host": "localhost", "dbname": "x"})
        assert conn is not None
        assert attempts == ["localhost", "127.0.0.1"]

    def test_a_real_host_is_not_second_guessed(self, monkeypatch):
        """Retrying 127.0.0.1 for a remote host would connect to the wrong
        database -- quietly, which is worse than failing."""
        import psycopg2

        from api import dbsetup

        attempts = []

        def fake_connect(**params):
            attempts.append(params["host"])
            raise psycopg2.OperationalError("nope")

        monkeypatch.setattr(dbsetup.psycopg2, "connect", fake_connect)
        try:
            dbsetup.connect({"host": "db.internal.example", "dbname": "x"})
            raise AssertionError("a non-loopback failure must propagate")
        except psycopg2.OperationalError:
            pass
        assert attempts == ["db.internal.example"], "a non-loopback host must be tried once"

    def test_the_probe_reports_which_address_answered(self, monkeypatch):
        """An invisible fallback would turn one confusing problem into a
        different confusing problem later, so /db/status carries it."""
        from api import dbsetup

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): self.sql = sql
            def fetchone(self):
                return ("PostgreSQL 16.15",) if "version" in self.sql else (1,)

        class Conn:
            def cursor(self): return Cur()
            def get_dsn_parameters(self): return {"host": "127.0.0.1"}
            def close(self): pass

        monkeypatch.setattr(dbsetup, "connect", lambda dsn, dbname=None: Conn())
        out = dbsetup.server_probe({"host": "localhost", "dbname": "txgraph"})
        assert out["reachable"] is True
        assert out["connected_host"] == "127.0.0.1"
