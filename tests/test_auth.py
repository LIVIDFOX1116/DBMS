"""
Offline tests for Google sign-in (api/auth.py + the gate in api/main.py).

Never reaches Google: the ID-token verifier is substituted. The point of
these tests is the parts that are ours -- what is gated, what a rejected
session looks like, and that the failure direction is closed.
"""
import os

os.environ["FRAMECHECK_API_MODE"] = "mock"

import importlib  # noqa: E402
from unittest import mock  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import auth  # noqa: E402
from api import main as api_main  # noqa: E402

SECRET = "test-session-secret-not-a-real-one"


@pytest.fixture
def required_client(monkeypatch):
    """A client with auth actually enforced."""
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "required")
    monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
    return TestClient(api_main.app, raise_server_exceptions=False)


@pytest.fixture
def signed_in(required_client, monkeypatch):
    token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
    required_client.cookies.set(auth.SESSION_COOKIE, token)
    return required_client


class TestAuthMode:
    def test_defaults_to_required_so_a_missing_env_var_fails_closed(self, monkeypatch):
        monkeypatch.delenv("FRAMECHECK_AUTH_MODE", raising=False)
        assert auth.auth_mode() == "required"

    def test_only_the_exact_word_disabled_turns_it_off(self, monkeypatch):
        # A typo must not silently open the API up.
        for value in ("", "no", "false", "0", "disable", "DISABLED "):
            monkeypatch.setenv("FRAMECHECK_AUTH_MODE", value)
            assert auth.auth_mode() == "required", value
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "DISABLED")
        assert auth.auth_mode() == "disabled"


class TestGate:
    def test_protected_route_without_a_session_is_401(self, required_client):
        resp = required_client.get("/wallets")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "sign-in required"}

    def test_health_and_benchmarks_stay_public(self, required_client):
        assert required_client.get("/health").status_code == 200
        assert required_client.get("/benchmarks").status_code == 200

    def test_a_valid_session_cookie_gets_through(self, signed_in):
        assert signed_in.get("/wallets").status_code == 200

    def test_a_bearer_token_also_works_for_the_desktop_app(self, required_client):
        token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
        resp = required_client.get("/wallets", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_a_tampered_session_is_rejected(self, required_client):
        token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
        required_client.cookies.set(auth.SESSION_COOKIE, token[:-4] + "aaaa")
        resp = required_client.get("/wallets")
        assert resp.status_code == 401

    def test_a_session_signed_with_another_secret_is_rejected(self, required_client, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", "a-different-secret")
        token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
        required_client.cookies.set(auth.SESSION_COOKIE, token)
        assert required_client.get("/wallets").status_code == 401

    def test_an_expired_session_is_rejected(self, required_client):
        token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
        required_client.cookies.set(auth.SESSION_COOKIE, token)
        with mock.patch.object(auth, "SESSION_TTL_SECONDS", -1):
            assert required_client.get("/wallets").status_code == 401

    def test_401_body_never_explains_why(self, required_client):
        # "bad signature" vs "expired" tells an attacker which half of the
        # token to work on; the reason belongs in the server log.
        token = auth.issue_session({"user_id": 7, "google_sub": "sub-123"})
        required_client.cookies.set(auth.SESSION_COOKIE, token[:-4] + "aaaa")
        body = required_client.get("/wallets").text
        assert "signature" not in body.lower()
        assert "expired" not in body.lower()


class TestSessionToken:
    def test_round_trips_the_user(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
        token = auth.issue_session({"user_id": 42, "google_sub": "sub-abc"})
        assert auth.read_session(token) == {"user_id": 42, "google_sub": "sub-abc"}

    def test_a_missing_secret_is_an_auth_error_not_a_blank_signing_key(self, monkeypatch):
        # Signing with "" would produce tokens anyone could forge.
        monkeypatch.delenv("FRAMECHECK_SESSION_SECRET", raising=False)
        with pytest.raises(auth.AuthError):
            auth.issue_session({"user_id": 1, "google_sub": "s"})


class TestPkce:
    def test_challenge_is_the_s256_of_the_verifier(self):
        import base64
        import hashlib

        verifier, challenge = auth.new_pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        assert challenge == expected

    def test_verifier_is_fresh_each_time_and_long_enough(self):
        # RFC 7636: 43-128 chars, and reuse would defeat the point.
        a, _ = auth.new_pkce_pair()
        b, _ = auth.new_pkce_pair()
        assert a != b
        assert 43 <= len(a) <= 128

    def test_no_padding_characters_leak_into_the_url_values(self):
        verifier, challenge = auth.new_pkce_pair()
        assert "=" not in verifier and "=" not in challenge


class TestGoogleCallback:
    def test_unverified_email_is_refused(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")

        def fake_verify(id_token, client_id):
            raise auth.AuthError("email not verified")

        monkeypatch.setattr(api_main, "_verify_id_token", fake_verify)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.post("/auth/callback", json={"id_token": "whatever"})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "sign-in failed"}

    def test_verification_failure_does_not_leak_the_reason(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")

        def fake_verify(id_token, client_id):
            raise ValueError("Token has wrong audience 1234-secret.apps.googleusercontent.com")

        monkeypatch.setattr(api_main, "_verify_id_token", fake_verify)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.post("/auth/callback", json={"id_token": "whatever"})
        assert resp.status_code == 401
        assert "audience" not in resp.text
        assert "googleusercontent" not in resp.text

    def test_login_is_503_when_the_server_has_no_google_client_configured(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.get("/auth/login")
        assert resp.status_code == 503

    def test_login_returns_pkce_parameters_but_never_a_client_secret(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "super-secret-value")
        client = TestClient(api_main.app, raise_server_exceptions=False)
        body = client.get("/auth/login").json()
        assert body["code_challenge_method"] == "S256"
        assert body["client_id"] == "test-client-id"
        assert "super-secret-value" not in str(body)


class TestSessionEndpoint:
    def test_reports_not_authenticated_instead_of_401(self, required_client):
        # The UI asks this before it has a session; a 401 here would make
        # "not signed in yet" indistinguishable from "something broke".
        body = required_client.get("/auth/session").json()
        assert body == {"authenticated": False, "auth_mode": "required", "user": None}

    def test_reports_disabled_mode_plainly(self, monkeypatch):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        client = TestClient(api_main.app, raise_server_exceptions=False)
        body = client.get("/auth/session").json()
        assert body["authenticated"] is True
        assert body["auth_mode"] == "disabled"


class TestUserIdentity:
    def test_user_is_keyed_on_google_sub_not_email(self):
        # A user who changes their Google email must stay the same row.
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1, "sub-1", "new@example.com", "N", None)
        auth.upsert_user(conn, {"sub": "sub-1", "email": "new@example.com", "name": "N"})
        sql = cur.execute.call_args.args[0]
        assert "ON CONFLICT (google_sub)" in sql
        assert "ON CONFLICT (email)" not in sql


def test_module_imports_cleanly_without_google_libraries_installed():
    # The CPU/offline path must never require the Google client libs at
    # import time -- only verify_google_id_token needs them.
    importlib.reload(auth)
