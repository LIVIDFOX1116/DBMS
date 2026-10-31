"""Role-based access control -- offline. The user lookup is patched; nothing
here reaches a database."""
import os
from unittest import mock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

os.environ["FRAMECHECK_API_MODE"] = "mock"

from api import auth, rbac  # noqa: E402
from api import main as api_main  # noqa: E402

SECRET = "rbac-test-secret"


def _all_route_keys():
    keys = set()
    for route in api_main.app.routes:
        if isinstance(route, (APIRoute, Route)):
            for method in route.methods - {"HEAD"}:
                keys.add((method, route.path))
    return keys


class TestEveryRouteHasAPolicy:
    def test_no_route_is_missing_from_the_policy_table(self):
        # A new endpoint must be given a role on purpose; until it is, it is
        # admin-only (fail closed) and this test fails to say so.
        missing = sorted(_all_route_keys() - set(rbac.ROUTE_POLICY))
        assert not missing, f"routes without an RBAC policy entry: {missing}"

    def test_no_policy_entry_names_a_route_that_does_not_exist(self):
        stale = sorted(set(rbac.ROUTE_POLICY) - _all_route_keys())
        assert not stale, f"policy entries for routes that no longer exist: {stale}"

    def test_unlisted_routes_fail_closed(self):
        scope = {"type": "http", "method": "GET", "path": "/wallets", "root_path": ""}
        with mock.patch.dict(rbac.ROUTE_POLICY, clear=True):
            assert rbac.required_role(api_main.app, scope) == rbac.ADMIN


def _scope(method, path):
    return {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}


class TestResolution:
    @pytest.mark.parametrize("method, path, role", [
        ("GET", "/wallets/5", rbac.VIEWER),
        ("HEAD", "/wallets", rbac.VIEWER),
        ("POST", "/jobs", rbac.ANALYST),
        ("POST", "/jobs/abc/cancel", rbac.ANALYST),
        ("GET", "/db/tables/wallet", rbac.ADMIN),
        ("POST", "/ingest/start", rbac.ADMIN),
        ("GET", "/health", rbac.PUBLIC),
        ("GET", "/dashboard.html", rbac.PUBLIC),        # the static frontend: no data in it
    ])
    def test_templates_resolve(self, method, path, role):
        assert rbac.required_role(api_main.app, _scope(method, path)) == role

    def test_docs_are_admin_only_when_deployed(self, monkeypatch):
        assert rbac.required_role(api_main.app, _scope("GET", "/openapi.json")) == rbac.PUBLIC
        monkeypatch.setenv("FRAMECHECK_HIDE_LANDING", "1")
        assert rbac.required_role(api_main.app, _scope("GET", "/openapi.json")) == rbac.ADMIN


@pytest.fixture
def live_client(monkeypatch):
    """Sign-in required, live mode, a fake job runner, and a user table
    that is a dict."""
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "required")
    monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
    monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
    monkeypatch.setattr(api_main, "using_mock", lambda: False)
    users = {}
    monkeypatch.setattr(rbac, "load_user", lambda uid: users.get(uid))
    job = mock.Mock()
    job.as_dict.return_value = {"job_id": "j1"}
    monkeypatch.setattr(api_main.jobs, "start", lambda *a, **k: job)
    client = TestClient(api_main.app, raise_server_exceptions=False)

    def as_user(uid, role, active=True):
        users[uid] = {"user_id": uid, "role": role, "username": f"u{uid}", "is_active": active}
        client.cookies.set(auth.SESSION_COOKIE, auth.issue_session({"user_id": uid, "google_sub": None}))
        return client
    return as_user


JOB = {"job_type": "detectors", "snapshot_id": 1, "seed_wallet_id": 1}


class TestRoleMatrix:
    def test_viewer(self, live_client):
        client = live_client(1, "viewer")
        assert client.get("/jobs").status_code == 200
        assert client.post("/jobs", json=JOB).status_code == 403
        assert client.get("/db/settings").status_code == 403

    def test_analyst(self, live_client):
        client = live_client(2, "analyst")
        assert client.post("/jobs", json=JOB).status_code == 200
        assert client.get("/db/settings").status_code == 403

    def test_admin(self, live_client):
        client = live_client(3, "admin")
        assert client.post("/jobs", json=JOB).status_code == 200
        assert client.get("/db/settings").status_code == 200

    def test_forbidden_says_which_role_is_needed(self, live_client):
        body = live_client(1, "viewer").get("/db/settings").json()
        assert body["required_role"] == "admin" and body["role"] == "viewer"

    def test_deactivated_user_is_401_on_the_very_next_request(self, live_client):
        client = live_client(4, "admin")
        assert client.get("/db/settings").status_code == 200
        live_client(4, "admin", active=False)
        assert client.get("/db/settings").status_code == 401

    def test_role_change_applies_immediately(self, live_client):
        # Read from the database each request, never from the token.
        client = live_client(5, "admin")
        assert client.get("/db/settings").status_code == 200
        live_client(5, "viewer")
        assert client.get("/db/settings").status_code == 403

    def test_session_for_a_deleted_user_is_401(self, live_client):
        client = live_client(6, "admin")
        client.cookies.set(auth.SESSION_COOKIE, auth.issue_session({"user_id": 999, "google_sub": None}))
        assert client.get("/jobs").status_code == 401

    def test_database_down_during_the_role_check_is_503(self, live_client, monkeypatch):
        from sqlalchemy import exc as sa_exc
        client = live_client(7, "admin")

        def _down(uid):
            raise sa_exc.OperationalError(None, None, Exception("down"))
        monkeypatch.setattr(rbac, "load_user", _down)
        assert client.get("/jobs").status_code == 503

    def test_static_pages_load_without_a_session(self, live_client):
        # The sign-in form has to load before anyone is signed in.
        client = live_client(8, "viewer")
        client.cookies.clear()
        assert client.get("/dashboard.html").status_code == 200
        assert client.get("/wallets").status_code == 401


class TestModesWithoutPerUserSessions:
    def test_auth_disabled_is_a_local_admin(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        client = TestClient(api_main.app)
        assert client.get("/db/settings").status_code == 200
        body = client.get("/auth/session").json()
        assert body["role"] == "admin"

    @pytest.mark.parametrize("auth_mode", ["disabled", "required"])
    def test_public_demo_is_an_anonymous_viewer(self, monkeypatch, auth_mode):
        # Including when sign-in is ALSO disabled -- the usual demo setup,
        # and the case that must not fall through to "local admin".
        monkeypatch.setenv("FRAMECHECK_PUBLIC_DEMO", "1")
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", auth_mode)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        assert client.get("/wallets").status_code == 200
        assert client.get("/db/settings").status_code == 403
        assert client.get("/auth/session").json()["role"] == "viewer"

    def test_mock_mode_session_is_a_viewer(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "required")
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        client.cookies.set(auth.SESSION_COOKIE, auth.issue_session({"user_id": 1, "google_sub": "s"}))
        assert client.get("/wallets").status_code == 200
        assert client.post("/jobs", json=JOB).status_code == 403
        assert client.get("/auth/session").json()["role"] == "viewer"


class TestGoogleSignInRules:
    @pytest.mark.parametrize("allowed, email, ok", [
        ("", "anyone@example.com", True),
        ("a@x.edu, b@y.com", "B@Y.com", True),
        ("a@x.edu", "c@x.edu", False),
        ("*@vit.ac.in", "student@vit.ac.in", True),
        ("*@vit.ac.in", "student@evil-vit.ac.in", False),
    ])
    def test_allowlist(self, monkeypatch, allowed, email, ok):
        monkeypatch.setenv("FRAMECHECK_ALLOWED_EMAILS", allowed)
        assert auth.email_allowed(email) is ok

    def test_admin_bootstrap_by_email_and_default_role(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_ADMIN_EMAILS", "boss@x.com")
        monkeypatch.delenv("FRAMECHECK_DEFAULT_ROLE", raising=False)
        assert auth.first_login_role("Boss@X.com") == "admin"
        assert auth.first_login_role("someone@x.com") == "viewer"
        monkeypatch.setenv("FRAMECHECK_DEFAULT_ROLE", "analyst")
        assert auth.first_login_role("someone@x.com") == "analyst"
        monkeypatch.setenv("FRAMECHECK_DEFAULT_ROLE", "superuser")       # nonsense falls back safely
        assert auth.first_login_role("someone@x.com") == "viewer"

    def _callback(self, monkeypatch, email, upserted=None):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
        monkeypatch.setattr(api_main, "_verify_id_token", lambda tok, cid: {"sub": "s1", "email": email})
        session_factory = mock.MagicMock()
        monkeypatch.setattr(api_main, "get_session", session_factory)
        monkeypatch.setattr(auth, "upsert_user", lambda db, claims: upserted)
        resp = TestClient(api_main.app, raise_server_exceptions=False).post("/auth/callback", json={"id_token": "t"})
        return resp, session_factory

    def test_address_off_the_allowlist_is_refused_before_any_row_is_written(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_ALLOWED_EMAILS", "team@x.com")
        resp, session_factory = self._callback(monkeypatch, "stranger@y.com")
        assert resp.status_code == 403
        session_factory.assert_not_called()

    def test_deactivated_google_account_is_refused(self, monkeypatch):
        monkeypatch.delenv("FRAMECHECK_ALLOWED_EMAILS", raising=False)
        user = {"user_id": 1, "google_sub": "s1", "email": "a@x", "display_name": None, "picture_url": None,
                "role": "viewer", "username": None, "is_active": False}
        resp, _ = self._callback(monkeypatch, "a@x", upserted=user)
        assert resp.status_code == 403 and "deactivated" in resp.json()["detail"]

    def test_first_sign_in_inserts_the_bootstrap_role(self, monkeypatch):
        from types import SimpleNamespace

        from sqlalchemy.dialects import postgresql
        monkeypatch.setenv("FRAMECHECK_ADMIN_EMAILS", "boss@x.com")
        session = mock.MagicMock()
        session.execute.return_value.one.return_value = SimpleNamespace(
            user_id=1, google_sub="s", email="boss@x.com", display_name=None, picture_url=None,
            role="admin", username=None, is_active=True)
        auth.upsert_user(session, {"sub": "s", "email": "boss@x.com"})
        params = session.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params
        assert params["role"] == "admin"


class TestPublicDemoGuard:
    @pytest.mark.parametrize("method, path", [
        ("PATCH", "/admin/users/1"), ("DELETE", "/admin/users/1"), ("PATCH", "/labels/1"), ("DELETE", "/labels/1"),
        ("PUT", "/anything"), ("POST", "/labels"), ("POST", "/wallets/5/labels"), ("DELETE", "/wallets/5/labels/1"),
        ("PATCH", "/alerts/3"), ("POST", "/auth/local/password"), ("POST", "/jobs"), ("POST", "/db/create"),
    ])
    def test_mutations_are_403_on_a_demo_link(self, monkeypatch, method, path):
        monkeypatch.setenv("FRAMECHECK_PUBLIC_DEMO", "1")
        resp = TestClient(api_main.app, raise_server_exceptions=False).request(method, path, json={})
        assert resp.status_code == 403 and "read-only public demo" in resp.json()["detail"]

    @pytest.mark.parametrize("path", ["/wallets", "/labels", "/alerts", "/wallets/1/labels"])
    def test_reads_still_work(self, monkeypatch, path):
        monkeypatch.setenv("FRAMECHECK_PUBLIC_DEMO", "1")
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        assert TestClient(api_main.app).get(path).status_code == 200
