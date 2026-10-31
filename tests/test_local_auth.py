"""Local accounts: bcrypt hashing, local login, password change, rate limit,
create_admin -- offline (mocked sessions, no database)."""
import os
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("FRAMECHECK_AUTH_MODE", "disabled")

from api import auth  # noqa: E402
from api import main as api_main  # noqa: E402
from db.orm.models import AppUser  # noqa: E402

GOOD = "correct horse battery"


@pytest.fixture(scope="module")
def good_hash():
    return auth.hash_password(GOOD)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    auth.login_limiter.reset()
    yield
    auth.login_limiter.reset()


class TestHashing:
    def test_bcrypt_cost_12(self, good_hash):
        assert good_hash.startswith("$2b$12$") and len(good_hash) == 60

    def test_right_and_wrong_password(self, good_hash):
        assert auth.verify_password(GOOD, good_hash)
        assert not auth.verify_password(GOOD + "!", good_hash)

    def test_no_hash_never_verifies(self):
        assert not auth.verify_password("anything at all", None)

    def test_malformed_hash_fails_closed(self):
        assert not auth.verify_password(GOOD, "not-a-bcrypt-hash")

    @pytest.mark.parametrize("pw", ["short", "x" * 11, "é" * 37])   # 37 x 2 bytes = 74 > 72
    def test_bad_new_passwords_are_refused(self, pw):
        with pytest.raises(ValueError):
            auth.hash_password(pw)

    def test_over_72_bytes_never_verifies(self, good_hash):
        # bcrypt would otherwise compare only the first 72 bytes.
        assert not auth.verify_password(GOOD + "x" * 80, good_hash)


class TestRateLimiter:
    def test_five_per_minute_then_refused(self):
        limiter = auth.RateLimiter(limit=5, window=60)
        assert all(limiter.allow("ip:1", now=t) for t in range(5))
        assert not limiter.allow("ip:1", now=5)

    def test_window_slides(self):
        limiter = auth.RateLimiter(limit=5, window=60)
        for t in range(5):
            limiter.allow("k", now=t)
        assert limiter.allow("k", now=65)

    def test_any_key_over_limit_refuses(self):
        # Per address AND per username: rotating addresses does not help an
        # attacker aiming at one account.
        limiter = auth.RateLimiter(limit=2, window=60)
        limiter.allow("ip:1", "user:alice", now=0)
        limiter.allow("ip:2", "user:alice", now=1)
        assert not limiter.allow("ip:3", "user:alice", now=2)


def _session_with(user):
    session = mock.MagicMock()
    session.scalars.return_value.one_or_none.return_value = user
    session.get.return_value = user
    return session


def _user(good_hash, **kw):
    fields = dict(user_id=5, username="alice", email="a@x", google_sub=None, display_name=None,
                  picture_url=None, password_hash=good_hash, role="analyst", is_active=True)
    fields.update(kw)
    return AppUser(**fields)


class TestLocalLogin:
    def test_success_returns_the_user_without_the_hash(self, good_hash):
        user = auth.local_login(_session_with(_user(good_hash)), "alice", GOOD)
        assert user["user_id"] == 5 and "password_hash" not in user

    @pytest.mark.parametrize("case", ["unknown", "wrong", "inactive", "no-password"])
    def test_every_failure_is_the_same_error(self, good_hash, case):
        user = {"unknown": None, "wrong": _user(good_hash), "inactive": _user(good_hash, is_active=False),
                "no-password": _user(good_hash, password_hash=None)}[case]
        password = GOOD + "x" if case == "wrong" else GOOD
        with pytest.raises(auth.AuthError, match=auth.LOGIN_FAILED):
            auth.local_login(_session_with(user), "alice", password)

    def test_unknown_user_still_costs_a_bcrypt_check(self):
        with mock.patch("bcrypt.checkpw", return_value=False) as checkpw, pytest.raises(auth.AuthError):
            auth.local_login(_session_with(None), "nobody", GOOD)
        checkpw.assert_called_once()


class TestEndpoints:
    def _client(self, monkeypatch, user):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)
        session = _session_with(user)
        monkeypatch.setattr(api_main, "get_session", lambda: _Ctx(session))
        monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", "test-secret")
        return TestClient(api_main.app, raise_server_exceptions=False)

    def test_login_issues_the_same_session_as_google(self, monkeypatch, good_hash):
        resp = self._client(monkeypatch, _user(good_hash)).post(
            "/auth/local/login", json={"username": "alice", "password": GOOD})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"user", "session_token"}
        assert set(body["user"]) == {"user_id", "email", "display_name", "picture_url"}
        assert "password_hash" not in resp.text and "$2b$" not in resp.text
        assert auth.read_session(body["session_token"]) == {"user_id": 5, "google_sub": ""}
        assert auth.SESSION_COOKIE in resp.cookies

    def test_wrong_password_is_a_generic_401(self, monkeypatch, good_hash):
        resp = self._client(monkeypatch, _user(good_hash)).post(
            "/auth/local/login", json={"username": "alice", "password": "nope-nope-nope"})
        assert resp.status_code == 401 and resp.json() == {"detail": auth.LOGIN_FAILED}

    def test_sixth_attempt_in_a_minute_is_429(self, monkeypatch, good_hash):
        client = self._client(monkeypatch, _user(good_hash))
        codes = [client.post("/auth/local/login", json={"username": "alice", "password": "wrong-wrong"}).status_code
                 for _ in range(6)]
        assert codes == [401] * 5 + [429]

    def test_mock_mode_has_no_local_accounts(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        resp = TestClient(api_main.app).post("/auth/local/login", json={"username": "a", "password": "b"})
        assert resp.status_code == 409

    def test_password_change_needs_a_signed_in_user(self, monkeypatch, good_hash):
        resp = self._client(monkeypatch, _user(good_hash)).post(
            "/auth/local/password", json={"current_password": GOOD, "new_password": "a-new-long-password"})
        assert resp.status_code == 409          # auth disabled here: there is no "me"


class TestChangePassword:
    def test_needs_the_current_password(self, good_hash):
        with pytest.raises(auth.AuthError):
            auth.change_password(_session_with(_user(good_hash)), 5, "wrong-current", "a-new-long-password")

    def test_stores_a_new_bcrypt_hash(self, good_hash):
        user = _user(good_hash)
        auth.change_password(_session_with(user), 5, GOOD, "a-new-long-password")
        assert auth.verify_password("a-new-long-password", user.password_hash)
        assert not auth.verify_password(GOOD, user.password_hash)


def test_sessions_for_local_accounts_round_trip(monkeypatch):
    monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", "s")
    token = auth.issue_session({"user_id": 9, "google_sub": None})
    assert auth.read_session(token) == {"user_id": 9, "google_sub": ""}


def test_the_api_module_never_touches_password_hash():
    # The hash is read and written only in api/auth.py. No endpoint module
    # may select or return it.
    source = (Path(api_main.__file__)).read_text(encoding="utf-8")
    assert "password_hash" not in source


class TestCreateAdmin:
    def test_password_is_prompted_and_confirmed(self):
        from scripts import create_admin
        answers = iter(["short", "a-long-enough-pass", "mismatch-mismatch", "a-long-enough-pass", "a-long-enough-pass"])
        assert create_admin.prompt_password(read=lambda prompt="": next(answers)) == "a-long-enough-pass"

    def test_existing_user_becomes_an_active_admin_with_the_same_id(self, good_hash):
        from scripts import create_admin
        user = _user(good_hash, role="viewer", is_active=False)
        uid, created = create_admin.upsert_admin(_session_with(user), "alice", "new@x", "HASH")
        assert (uid, created) == (5, False)
        assert (user.role, user.is_active, user.password_hash, user.email) == ("admin", True, "HASH", "new@x")

    def test_new_user_is_created(self):
        from scripts import create_admin
        session = _session_with(None)
        _, created = create_admin.upsert_admin(session, "bob", "b@x", "HASH")
        assert created
        added = session.add.call_args.args[0]
        assert (added.username, added.role, added.is_active) == ("bob", "admin", True)


class _Ctx:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *a):
        return False
