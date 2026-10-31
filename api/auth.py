"""
FrameCheck: Google sign-in (OAuth 2.0 + PKCE) and session issuing.

Two different clients, one backend:

- the browser dashboard does the standard redirect flow and gets an
  HttpOnly session cookie;
- the Tauri desktop app does the loopback flow (redirect to
  http://127.0.0.1:<port>, a port it opens for one request) and gets the
  same session token as a bearer, because a packaged desktop app has no
  cookie jar shared with the sidecar.

Both use PKCE. The desktop app is a public client -- it cannot keep a
client secret, since anything shipped inside the bundle is readable by
whoever has the bundle -- so PKCE is what actually ties the authorization
code to the client that requested it.

What this module deliberately does NOT do: it never trusts an ID token's
payload without verifying the signature against Google's published keys,
and it never keys a user on their email address (see migration 006 for
why `sub` is the identity). The signature check is injectable
(`verify_google_id_token`) purely so the test suite can run offline --
the injection point is for tests, never a way to skip verification in a
real run.
"""
import logging
import os

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.orm.models import AppUser

log = logging.getLogger("framecheck.auth")

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Routes reachable without a session. /health is the liveness probe the
# desktop shell polls before the user has signed in (and what tells them
# the backend is up at all); /benchmarks serves machine timings from a
# checked-in file, not wallet data.
PUBLIC_PATHS = frozenset({"/health", "/benchmarks", "/auth/login", "/auth/callback", "/auth/session", "/docs", "/openapi.json", "/redoc"})

SESSION_COOKIE = "framecheck_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


class AuthError(Exception):
    """Anything that should become a 401 -- never surfaced verbatim to the
    caller, since the detail can describe why a token failed."""


def auth_mode() -> str:
    """'required' (the default) or 'disabled'. Disabling is a deliberate,
    explicit act: a tool that gates access by default and opens up only
    when told to is the right way round, so a missing or misspelled
    env var fails closed."""
    return "disabled" if os.getenv("FRAMECHECK_AUTH_MODE", "required").lower() == "disabled" else "required"


def public_demo_mode() -> bool:
    """Whether this process is serving a read-only public link (shared via
    an ngrok/Cloudflare tunnel, for someone outside the team to click
    around in live data with no login wall and no way to change anything).

    Only the literal value '1' opts in -- same fail-closed shape as
    auth_mode(), but inverted: here the *safe* default is doing nothing,
    so an unset or misspelled var just means "not a public demo" rather
    than "accidentally wide open". Implies auth_mode() == 'disabled' (no
    sign-in wall for a link you're handing to someone with no account) --
    api.main's middleware checks this directly rather than mutating
    FRAMECHECK_AUTH_MODE, so the two env vars never fight over who set what.
    """
    return os.getenv("FRAMECHECK_PUBLIC_DEMO", "0").strip() == "1"


def session_secret() -> str:
    secret = os.getenv("FRAMECHECK_SESSION_SECRET", "")
    if not secret:
        raise AuthError("FRAMECHECK_SESSION_SECRET is not set")
    return secret


def google_client_id() -> str:
    return os.getenv("GOOGLE_CLIENT_ID", "")


def verify_google_id_token(id_token: str, client_id: str) -> dict:
    """Verifies the ID token's signature against Google's published keys
    and returns its claims. Network-dependent by nature (Google rotates
    the signing keys), which is why api/main.py resolves it through
    `_verifier` -- the tests substitute a stub there rather than reaching
    the internet."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    claims = google_id_token.verify_oauth2_token(id_token, google_requests.Request(), client_id)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("unexpected issuer")
    if not claims.get("email_verified"):
        # An unverified email means Google has not confirmed the person
        # controls that address, so it must not be shown as an identity.
        raise AuthError("email not verified")
    return claims


def _user_dict(user) -> dict:
    return {"user_id": user.user_id, "google_sub": user.google_sub, "email": user.email,
            "display_name": user.display_name, "picture_url": user.picture_url}


def upsert_user(session, claims: dict) -> dict:
    """Creates or refreshes the signed-in user. Keyed on `sub`, so a user
    who changes their Google email keeps the same row rather than
    silently becoming a second account.

    One statement through the ORM: PostgreSQL's INSERT ... ON CONFLICT
    (google_sub) DO UPDATE ... RETURNING, built from the AppUser model, so
    two concurrent first sign-ins by the same person cannot race into a
    duplicate the way a get-then-insert would."""
    insert = pg_insert(AppUser).values(
        google_sub=claims["sub"],
        email=claims.get("email", ""),
        display_name=claims.get("name"),
        picture_url=claims.get("picture"),
        last_login_at=func.now(),
    )
    statement = insert.on_conflict_do_update(
        index_elements=[AppUser.google_sub],
        set_={
            "email": insert.excluded.email,
            "display_name": insert.excluded.display_name,
            "picture_url": insert.excluded.picture_url,
            "last_login_at": func.now(),
        },
    ).returning(AppUser.user_id, AppUser.google_sub, AppUser.email,
                AppUser.display_name, AppUser.picture_url)
    row = session.execute(statement).one()
    # Committed here, before the caller issues a session token for this
    # user -- the token must never name a row that could still roll back.
    session.commit()
    return _user_dict(row)


def issue_session(user: dict, now: float | None = None) -> str:
    """A signed, expiring session token. itsdangerous gives tamper
    detection with a server-side secret; there is no revocation list
    because sessions are short-lived and this is a single-operator local
    tool, not a multi-tenant service."""
    from itsdangerous import TimestampSigner

    signer = TimestampSigner(session_secret())
    payload = f"{user['user_id']}:{user['google_sub']}"
    return signer.sign(payload).decode()


def read_session(token: str, max_age: int | None = None) -> dict:
    from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

    # Read the TTL at call time: as a default argument it would be frozen
    # at import, so changing it (config, tests) would silently do nothing.
    if max_age is None:
        max_age = SESSION_TTL_SECONDS
    signer = TimestampSigner(session_secret())
    try:
        payload = signer.unsign(token, max_age=max_age).decode()
    except SignatureExpired as e:
        raise AuthError("session expired") from e
    except BadSignature as e:
        raise AuthError("bad session signature") from e
    user_id, _, google_sub = payload.partition(":")
    return {"user_id": int(user_id), "google_sub": google_sub}


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for RFC 7636 S256. The verifier never leaves
    the client that generated it; only its SHA-256 hash goes to Google,
    so an intercepted authorization code is useless without it."""
    import base64
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def pg_user_fetch(session, user_id: int) -> dict | None:
    """The signed-in user by primary key -- a plain identity-map lookup."""
    user = session.get(AppUser, user_id)
    return None if user is None else _user_dict(user)


__all__ = [
    "AuthError", "PUBLIC_PATHS", "SESSION_COOKIE", "SESSION_TTL_SECONDS",
    "auth_mode", "public_demo_mode", "google_client_id", "issue_session", "new_pkce_pair",
    "pg_user_fetch", "read_session", "session_secret", "upsert_user",
    "verify_google_id_token",
]
