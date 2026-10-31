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
        session.execute.return_value.one.return_value = SimpleNamespace(**row)
        return session

    def test_upsert_returns_the_same_dict_as_before(self):
        session = self._session_returning(user_id=7, google_sub="s", email="e@x", display_name="D", picture_url="p")
        user = auth.upsert_user(session, {"sub": "s", "email": "e@x", "name": "D", "picture": "p"})
        assert user == {"user_id": 7, "google_sub": "s", "email": "e@x", "display_name": "D", "picture_url": "p"}

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
                            "app_user.display_name, app_user.picture_url")

    def test_upsert_binds_values_rather_than_interpolating_them(self):
        session = self._session_returning(user_id=1, google_sub="x", email="", display_name=None, picture_url=None)
        hostile = "'); DROP TABLE app_user; --"
        auth.upsert_user(session, {"sub": hostile, "email": hostile})
        statement = session.execute.call_args.args[0]
        assert hostile not in compiled(statement)
        assert hostile in statement.compile(dialect=PG).params.values()

    def test_fetch_is_a_primary_key_lookup(self):
        session = mock.MagicMock()
        session.get.return_value = AppUser(user_id=3, google_sub="s", email="e", display_name=None, picture_url=None)
        assert auth.pg_user_fetch(session, 3) == {
            "user_id": 3, "google_sub": "s", "email": "e", "display_name": None, "picture_url": None}
        session.get.assert_called_once_with(AppUser, 3)

    def test_fetch_of_a_missing_user_is_none(self):
        session = mock.MagicMock()
        session.get.return_value = None
        assert auth.pg_user_fetch(session, 404) is None
