"""GET/PATCH/DELETE /admin/users and the pagination helper -- offline: the user
lookup and the session are fakes."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import postgresql

os.environ["FRAMECHECK_API_MODE"] = "mock"

from api import auth, pagination, rbac  # noqa: E402
from api import main as api_main  # noqa: E402

SECRET = "admin-test-secret"
PG = postgresql.dialect()


def compiled(statement):
    return " ".join(str(statement.compile(dialect=PG)).split())


def _row(**fields):
    return SimpleNamespace(_mapping=fields)


class FakeSession:
    def __init__(self, rows=(), total=None, error=None):
        self.statements, self.rows, self.total, self.error = [], list(rows), total, error

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if self.error is not None and "CALL" in str(statement):
            raise self.error
        result = mock.MagicMock()
        result.__iter__.return_value = iter(self.rows)
        result.first.return_value = self.rows[0] if self.rows else None
        return result

    def scalar(self, statement):
        self.statements.append((statement, None))
        return self.total

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def procedure_error(code, message):
    orig = Exception(message)
    orig.pgcode = code
    orig.diag = SimpleNamespace(message_primary=message)
    return sa_exc.ProgrammingError("CALL", {}, orig)


@pytest.fixture
def admin(monkeypatch):
    """A client signed in as admin user 1, in live mode. Returns (client, set_session)."""
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "required")
    monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
    monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
    monkeypatch.setattr(api_main, "using_mock", lambda: False)
    monkeypatch.setattr(rbac, "load_user", lambda uid: {"user_id": uid, "role": "admin", "username": "a", "is_active": True})
    client = TestClient(api_main.app, raise_server_exceptions=False)
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session({"user_id": 1, "google_sub": None}))

    def use(session):
        monkeypatch.setattr(api_main, "get_session", lambda: session)
        return session
    return client, use


class TestList:
    def test_selects_no_password_hash_and_sets_headers(self, admin):
        client, use = admin
        session = use(FakeSession(total=120, rows=[_row(user_id=2, email="x@y", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))]))
        resp = client.get("/admin/users", params={"page": 2, "page_size": 50})
        assert resp.status_code == 200
        assert resp.json()[0]["user_id"] == 2
        assert resp.headers["X-Total-Count"] == "120" and resp.headers["X-Page"] == "2" and resp.headers["X-Page-Size"] == "50"
        assert 'rel="next"' in resp.headers["Link"] and 'rel="prev"' in resp.headers["Link"]
        for statement, _ in session.statements:
            assert "password_hash" not in compiled(statement)

    def test_search_is_bound_and_escaped(self, admin):
        client, use = admin
        session = use(FakeSession(total=0))
        client.get("/admin/users", params={"q": "100%_'; DROP TABLE app_user"})
        statement = session.statements[-1][0]
        assert "DROP TABLE" not in compiled(statement)
        assert "ILIKE" in compiled(statement).upper() or "lower(" in compiled(statement).lower()
        assert any("DROP TABLE" in str(v) for v in statement.compile(dialect=PG).params.values())

    def test_mock_mode_has_no_users_but_keeps_the_shape(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        resp = TestClient(api_main.app).get("/admin/users")
        assert resp.status_code == 200 and resp.json() == [] and resp.headers["X-Total-Count"] == "0"


class TestUpdate:
    def test_role_change_goes_through_the_procedure(self, admin):
        client, use = admin
        session = use(FakeSession(rows=[_row(user_id=2, role="analyst")]))
        resp = client.patch("/admin/users/2", json={"role": "analyst"})
        assert resp.status_code == 200 and resp.json()["role"] == "analyst"
        statement, params = session.statements[0]
        assert str(statement) == "CALL sp_set_user_role(:actor, :target, :role)"
        assert params == {"actor": 1, "target": 2, "role": "analyst"}

    def test_activation_goes_through_its_procedure(self, admin):
        client, use = admin
        session = use(FakeSession(rows=[_row(user_id=2)]))
        client.patch("/admin/users/2", json={"is_active": False})
        assert str(session.statements[0][0]) == "CALL sp_set_user_active(:actor, :target, :active)"
        assert session.statements[0][1]["active"] is False

    @pytest.mark.parametrize("code, status", [("42501", 403), ("22023", 422), ("P0002", 404), ("23001", 409), ("23514", 409)])
    def test_procedure_errors_map_to_http(self, admin, code, status):
        client, use = admin
        use(FakeSession(error=procedure_error(code, "from the procedure")))
        assert client.patch("/admin/users/2", json={"role": "viewer"}).status_code == status

    def test_last_admin_refusal_is_explained(self, admin):
        client, use = admin
        use(FakeSession(error=procedure_error("23001", "cannot demote the last active admin (user 1)")))
        assert "last active admin" in client.patch("/admin/users/1", json={"role": "viewer"}).json()["detail"]

    def test_other_database_errors_are_not_disguised(self, admin):
        client, use = admin
        use(FakeSession(error=procedure_error("XX000", "secret internal detail")))
        resp = client.patch("/admin/users/2", json={"role": "viewer"})
        assert resp.status_code == 500 and "secret" not in resp.text

    def test_unknown_role_is_422_before_the_database(self, admin):
        client, use = admin
        session = use(FakeSession())
        assert client.patch("/admin/users/2", json={"role": "root"}).status_code == 422
        assert session.statements == []

    def test_nothing_to_change_is_422(self, admin):
        client, use = admin
        use(FakeSession())
        assert client.patch("/admin/users/2", json={}).status_code == 422

    def test_needs_a_real_signed_in_admin(self, monkeypatch):
        # Sign-in disabled: a local admin with no app_user row to act as.
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        resp = TestClient(api_main.app).patch("/admin/users/2", json={"role": "viewer"})
        assert resp.status_code == 409

    def test_mock_mode_refuses_mutations(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        resp = TestClient(api_main.app).delete("/admin/users/2")
        assert resp.status_code == 409 and "live mode" in resp.json()["detail"]


def test_delete_is_a_soft_delete(admin):
    client, use = admin
    session = use(FakeSession())
    resp = client.delete("/admin/users/2")
    assert resp.json() == {"status": "deactivated", "user_id": 2}
    assert str(session.statements[0][0]) == "CALL sp_set_user_active(:actor, :target, FALSE)"


def test_only_admins_can_reach_any_of_it(admin, monkeypatch):
    client, use = admin
    use(FakeSession())
    monkeypatch.setattr(rbac, "load_user", lambda uid: {"user_id": uid, "role": "analyst", "username": "a", "is_active": True})
    assert client.get("/admin/users").status_code == 403
    assert client.patch("/admin/users/2", json={"role": "admin"}).status_code == 403
    assert client.delete("/admin/users/2").status_code == 403


class TestPaginationHelper:
    def _page(self, rows, **kw):
        request = mock.MagicMock()
        request.url.include_query_params.side_effect = lambda **q: f"http://x/y?page={q['page']}&page_size={q['page_size']}"
        response = SimpleNamespace(headers={})
        return pagination.page_of_list(rows, request, response, kw.get("page"), kw.get("page_size")), response.headers

    def test_pages_do_not_overlap_and_cover_everything(self):
        seen = []
        for page in (1, 2, 3):
            rows, _ = self._page(list(range(25)), page=page, page_size=10)
            seen += rows
        assert seen == list(range(25))

    def test_link_header(self):
        _, first = self._page(list(range(25)), page=1, page_size=10)
        assert 'rel="next"' in first["Link"] and 'rel="prev"' not in first["Link"]
        _, last = self._page(list(range(25)), page=3, page_size=10)
        assert 'rel="prev"' in last["Link"] and 'rel="next"' not in last["Link"]
        _, only = self._page([1], page=1, page_size=10)
        assert "Link" not in only

    @pytest.mark.parametrize("page, size", [(0, 10), (1, 0), (1, 501)])
    def test_bad_values_are_422(self, page, size):
        with pytest.raises(api_main.HTTPException) as e:
            pagination.normalise(page, size)
        assert e.value.status_code == 422

    def test_defaults(self):
        assert pagination.normalise(None, None) == (1, 50)
        assert not pagination.requested(None, None) and pagination.requested(2, None)

    def test_headers_are_exposed_to_cross_origin_scripts(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        resp = TestClient(api_main.app).get("/admin/users", headers={"Origin": "http://localhost:5500"})
        exposed = resp.headers["access-control-expose-headers"]
        assert "X-Total-Count" in exposed and "Link" in exposed
