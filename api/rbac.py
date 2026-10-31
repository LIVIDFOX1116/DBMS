"""Role-based access control: one central route -> minimum-role table.

Roles are ordered viewer < analyst < admin:

    viewer   every read-only view and GET endpoint, CSV export
    analyst  viewer + run analysis jobs (POST /jobs, cancel), manage labels
             and wallet tags, review alerts
    admin    analyst + the Database view (/db/*), Etherscan ingest
             (/ingest/*), user management (/admin/*), the internal hooks

Enforced in api.main's `require_session` middleware rather than as per-route
dependencies, so a route added later is protected by default: a route
missing from ROUTE_POLICY is treated as admin-only (fail closed), and
tests/test_rbac.py walks app.routes and fails on any route without an entry
-- an endpoint cannot be accidentally left open, or accidentally locked.

The role is read from app_user on EVERY request (one primary-key lookup),
never from the signed session token, so a role change or a deactivation
takes effect on the user's very next request.
"""
import os

from starlette.routing import Match, Mount

PUBLIC = "public"
VIEWER = "viewer"
ANALYST = "analyst"
ADMIN = "admin"
ROLES = (VIEWER, ANALYST, ADMIN)
RANK = {VIEWER: 1, ANALYST: 2, ADMIN: 3}

# Who a request acts as when there is no per-user session to read.
# FRAMECHECK_AUTH_MODE=disabled is the desktop sidecar (loopback + per-launch
# token) and local development: it behaves exactly as before roles existed.
LOCAL_ADMIN = {"user_id": None, "role": ADMIN, "username": None, "is_active": True}
# FRAMECHECK_PUBLIC_DEMO=1: an anonymous, read-only visitor.
DEMO_VIEWER = {"user_id": None, "role": VIEWER, "username": None, "is_active": True}


def deployed() -> bool:
    """A deployed instance (FRAMECHECK_HIDE_LANDING=1) keeps its API docs for
    admins only; locally they stay public, as they always were."""
    return os.getenv("FRAMECHECK_HIDE_LANDING", "0").strip() == "1"


def _docs() -> str:
    return ADMIN if deployed() else PUBLIC


# (method, route template) -> minimum role. Every route the app registers
# must appear here; see the module docstring.
ROUTE_POLICY = {
    # --- public: liveness, sign-in, machine timings from a checked-in file
    ("GET", "/health"): PUBLIC,
    ("GET", "/benchmarks"): PUBLIC,
    ("GET", "/auth/login"): PUBLIC,
    ("POST", "/auth/callback"): PUBLIC,
    ("POST", "/auth/local/login"): PUBLIC,
    ("GET", "/auth/session"): PUBLIC,
    ("GET", "/openapi.json"): "docs",
    ("GET", "/docs"): "docs",
    ("GET", "/docs/oauth2-redirect"): "docs",
    ("GET", "/redoc"): "docs",

    # --- viewer: everything read-only, plus your own account
    ("POST", "/auth/logout"): VIEWER,
    ("POST", "/auth/local/password"): VIEWER,
    ("GET", "/stats/overview"): VIEWER,
    ("GET", "/wallets"): VIEWER,
    ("GET", "/wallets/{wallet_id}"): VIEWER,
    ("GET", "/clusters"): VIEWER,
    ("GET", "/clusters/top-flagged"): VIEWER,
    ("GET", "/clusters/{cluster_id}/subgraph"): VIEWER,
    ("GET", "/clusters/{cluster_id}"): VIEWER,
    ("GET", "/cluster-membership"): VIEWER,
    ("GET", "/clusters/{cluster_id}/members"): VIEWER,
    ("GET", "/alerts"): VIEWER,
    ("GET", "/snapshots"): VIEWER,
    ("GET", "/snapshots/{snapshot_id}/edges"): VIEWER,
    ("GET", "/runs/{run_id}/metrics"): VIEWER,
    ("GET", "/flags"): VIEWER,
    ("GET", "/flags/{flag_id}/evidence"): VIEWER,
    ("GET", "/flags/{flag_id}/evidence-edges"): VIEWER,
    ("GET", "/flags/{flag_id}/audit"): VIEWER,
    ("GET", "/flags/{flag_id}/audit.csv"): VIEWER,
    ("GET", "/flags/{flag_id}/versions"): VIEWER,
    ("GET", "/flags/as-of"): VIEWER,
    ("GET", "/audit/verdicts"): VIEWER,
    ("GET", "/audit/overview"): VIEWER,
    ("GET", "/audit/trail"): VIEWER,
    ("GET", "/audit/summary"): VIEWER,
    ("GET", "/jobs"): VIEWER,
    ("GET", "/jobs/{job_id}"): VIEWER,

    # --- analyst: run analyses
    ("POST", "/jobs"): ANALYST,
    ("POST", "/jobs/{job_id}/cancel"): ANALYST,

    # --- admin: the database, ingestion, the desktop shell's internal hooks
    ("POST", "/internal/api-key"): ADMIN,
    ("POST", "/internal/db-settings"): ADMIN,
    ("GET", "/db/settings"): ADMIN,
    ("GET", "/db/status"): ADMIN,
    ("POST", "/db/create"): ADMIN,
    ("POST", "/db/schema"): ADMIN,
    ("POST", "/db/adopt"): ADMIN,
    ("GET", "/db/tables"): ADMIN,
    ("GET", "/db/tables/{table}/count"): ADMIN,
    ("GET", "/db/tables/{table}"): ADMIN,
    ("GET", "/db/datasets"): ADMIN,
    ("POST", "/db/import"): ADMIN,
    ("POST", "/db/mode"): ADMIN,
    ("GET", "/ingest/plan"): ADMIN,
    ("POST", "/ingest/start"): ADMIN,
    ("GET", "/admin/users"): ADMIN,
    ("PATCH", "/admin/users/{user_id}"): ADMIN,
    ("DELETE", "/admin/users/{user_id}"): ADMIN,
}


def allows(role: str | None, needed: str) -> bool:
    if needed == PUBLIC:
        return True
    return RANK.get(role or "", 0) >= RANK[needed]


def required_role(app, scope) -> str:
    """The minimum role for this request, resolved against the app's own
    routing (so `/wallets/5` finds the `/wallets/{wallet_id}` entry).

    - the static frontend mount is public: the pages hold no data, and the
      sign-in form has to load before anyone is signed in;
    - a path that exists but not for this method (FastAPI will answer 405)
      and a path that matches nothing (404) still need a session, so the API
      cannot be mapped by an anonymous caller;
    - a route missing from ROUTE_POLICY is admin-only.
    """
    method = "GET" if scope["method"] == "HEAD" else scope["method"]
    for route in app.router.routes:
        match, _ = route.matches({**scope, "method": method})
        if match == Match.FULL:
            if isinstance(route, Mount):
                return PUBLIC
            needed = ROUTE_POLICY.get((method, route.path), ADMIN)
            return _docs() if needed == "docs" else needed
    return VIEWER


def load_user(user_id: int) -> dict | None:
    """The signed-in user's current role and status, straight from app_user."""
    from db.orm.models import AppUser
    from db.orm.session import get_session

    with get_session() as db:
        user = db.get(AppUser, user_id)
        if user is None:
            return None
        return {"user_id": user.user_id, "role": user.role, "username": user.username,
                "is_active": user.is_active}
