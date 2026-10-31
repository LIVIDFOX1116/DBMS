"""Migration 010 (analyst folded into app_user) and the code that has to
follow it -- offline. The migration itself is exercised against a real
PostgreSQL by tests/test_orm_integration.py when FRAMECHECK_TEST_PG_DSN is set.
"""
import re
from pathlib import Path
from unittest import mock

from api import dbsetup

MIGRATION = Path(__file__).resolve().parent.parent / "db" / "migrations" / "010_single_users_table_and_roles.sql"


def _statements(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


class TestMigrationFile:
    sql = MIGRATION.read_text(encoding="utf-8")

    def test_the_drop_is_alone_in_the_last_transaction(self):
        # The fold commits first; the destructive step stands apart, so it can
        # be refused without undoing the rest.
        blocks = re.findall(r"BEGIN;(.*?)COMMIT;", _statements(self.sql), re.S)
        assert blocks[-1].strip() == "DROP TABLE IF EXISTS analyst;"
        assert all("DROP TABLE" not in b for b in blocks[:-1])

    def test_ids_are_remapped_only_while_the_old_foreign_key_exists(self):
        # Re-running must never remap ids that were already remapped.
        do_block = re.search(r"DO \$\$(.*?)END \$\$;", self.sql, re.S).group(1)
        assert "conname = 'analysis_run_analyst_id_fkey'" in do_block
        assert "confrelid = to_regclass('analyst')" in do_block
        assert do_block.count("UPDATE ") == 3

    def test_the_service_account_cannot_sign_in(self):
        assert "FALSE,                             -- no analyst row could ever sign in" in self.sql
        assert "CHECK (google_sub IS NOT NULL OR password_hash IS NOT NULL OR NOT is_active)" in self.sql

    def test_every_foreign_key_now_targets_app_user(self):
        added = re.findall(r"ADD\s+CONSTRAINT (\w+) FOREIGN KEY \((\w+)\) REFERENCES (\w+)", self.sql)
        assert {(c, t) for _, c, t in added} == {("analyst_id", "app_user"), ("tagged_by", "app_user"),
                                                 ("reviewed_by", "app_user")}

    def test_declares_a_probe_for_adopt(self):
        assert dbsetup.migration_probe(MIGRATION).startswith("SELECT to_regclass('analyst') IS NULL")


class TestDatasetCompatibility:
    """Datasets carry an `analyst` block. After 010 it is a dictionary of
    id -> username resolved against app_user, never loaded."""

    def _conn(self, analyst_table_exists: bool, names=()):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (not analyst_table_exists,)
        cur.fetchall.return_value = list(names)
        cur.rowcount = 1
        return conn, cur

    def test_analyst_block_resolves_against_app_user_once_folded(self):
        conn, cur = self._conn(analyst_table_exists=False)
        assert dbsetup._seed_target(conn, "analyst") == ("app_user", "user_id", "username")

    def test_before_010_the_analyst_table_is_still_the_target(self):
        conn, _ = self._conn(analyst_table_exists=True)
        assert dbsetup._seed_target(conn, "analyst") == ("analyst", "analyst_id", "username")

    def test_names_resolve_to_user_ids(self):
        conn, cur = self._conn(analyst_table_exists=False, names=[("system", 3)])
        assert dbsetup._target_seed_names(conn, "analyst") == {"system": 3}
        sql = repr(cur.execute.call_args_list[-1].args[0])
        assert "app_user" in sql and "username" in sql

    def test_unknown_names_get_inactive_placeholder_accounts(self):
        conn, cur = self._conn(analyst_table_exists=False)
        created = dbsetup._ensure_placeholder_users(conn, ["1\tsystem\n", "7\tcarol\n"], ["analyst_id", "username"])
        assert created == 2
        statements = [c.args for c in cur.execute.call_args_list]
        for sql, params in statements:
            assert "INSERT INTO app_user" in sql and "'viewer', FALSE" in sql and "ON CONFLICT (username)" in sql
            assert params[1].endswith("@imported.invalid")
        assert [p[0] for _, p in statements] == ["carol", "system"]

    def test_ids_map_through_username(self):
        mapping = dbsetup._seed_id_map(["1\tsystem\n"], ["analyst_id", "username"], "analyst", {"system": 3})
        assert mapping == {"1": "3"}


def test_start_run_resolves_the_system_account_by_username():
    from graph.run_analysis import start_run

    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [(1,), (42,)]          # algorithm_id, then the new run_id
    assert start_run(conn, 1, "pagerank") == 42
    sql, params = cur.execute.call_args_list[-1].args
    assert "to_jsonb(u)->>'username' = 'system'" in sql
    assert params[2] is None, "no explicit owner: the database resolves the system account"
