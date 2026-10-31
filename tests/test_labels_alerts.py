"""Labels CRUD, wallet tags, alert review -- offline, against a fake session."""
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import postgresql

os.environ["FRAMECHECK_API_MODE"] = "mock"

from api import auth, rbac  # noqa: E402
from api import main as api_main  # noqa: E402
from db.orm.models import Alert, Label, Wallet, WalletLabel  # noqa: E402

SECRET = "labels-test-secret"
PG = postgresql.dialect()


class FakeSession:
    """Objects keyed by (model, primary key); records adds, deletes, statements."""

    def __init__(self, objects=None, scalar=0, rows=(), flush_error=None):
        self.objects, self.scalar_value, self.rows, self.flush_error = objects or {}, scalar, list(rows), flush_error
        self.added, self.deleted, self.statements = [], [], []

    def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def flush(self):
        if self.flush_error is not None:
            raise self.flush_error
        for obj in self.added:
            if isinstance(obj, Label) and obj.label_id is None:
                obj.label_id = 99

    def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_value

    def execute(self, statement, params=None):
        self.statements.append(statement)
        result = mock.MagicMock()
        result.__iter__.return_value = iter(self.rows)
        result.first.return_value = self.rows[0] if self.rows else None
        return result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def signed_in(monkeypatch):
    """(client, set_role, use_session): live mode, sign-in required."""
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "required")
    monkeypatch.setenv("FRAMECHECK_SESSION_SECRET", SECRET)
    monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
    monkeypatch.setattr(api_main, "using_mock", lambda: False)
    role = {"value": "analyst"}
    monkeypatch.setattr(rbac, "load_user", lambda uid: {"user_id": uid, "role": role["value"], "username": "u", "is_active": True})
    client = TestClient(api_main.app, raise_server_exceptions=False)
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session({"user_id": 7, "google_sub": None}))

    def use(session):
        monkeypatch.setattr(api_main, "get_session", lambda: session)
        return session
    return client, role, use


def _duplicate():
    orig = Exception("duplicate key")
    orig.pgcode = "23505"
    return sa_exc.IntegrityError("INSERT", {}, orig)


class TestLabels:
    def test_create(self, signed_in):
        client, _, use = signed_in
        session = use(FakeSession())
        resp = client.post("/labels", json={"label_name": "Mixer", "category": "risk", "is_sanctioned": True})
        assert resp.status_code == 201
        assert resp.json() == {"label_id": 99, "label_name": "Mixer", "category": "risk", "source": None, "is_sanctioned": True}
        assert session.added[0].label_name == "Mixer"

    def test_duplicate_name_is_409(self, signed_in):
        client, _, use = signed_in
        use(FakeSession(flush_error=_duplicate()))
        assert client.post("/labels", json={"label_name": "Mixer"}).status_code == 409

    @pytest.mark.parametrize("body", [{}, {"label_name": ""}, {"label_name": "x" * 61}, {"label_name": "ok", "category": "x" * 31}])
    def test_bad_input_is_422(self, signed_in, body):
        client, _, use = signed_in
        use(FakeSession())
        assert client.post("/labels", json=body).status_code == 422

    def test_get_and_404(self, signed_in):
        client, _, use = signed_in
        use(FakeSession({(Label, 1): Label(label_id=1, label_name="A", is_sanctioned=False)}))
        assert client.get("/labels/1").json()["label_name"] == "A"
        assert client.get("/labels/2").status_code == 404

    def test_patch_changes_only_what_was_sent(self, signed_in):
        client, _, use = signed_in
        label = Label(label_id=1, label_name="A", category="c", is_sanctioned=False)
        use(FakeSession({(Label, 1): label}))
        resp = client.patch("/labels/1", json={"category": "d"})
        assert resp.status_code == 200 and (label.label_name, label.category) == ("A", "d")

    def test_patch_nothing_or_null_name_is_422(self, signed_in):
        client, _, use = signed_in
        use(FakeSession({(Label, 1): Label(label_id=1, label_name="A")}))
        assert client.patch("/labels/1", json={}).status_code == 422
        assert client.patch("/labels/1", json={"label_name": None}).status_code == 422

    def test_delete_attached_label_is_409_and_keeps_everything(self, signed_in):
        client, _, use = signed_in
        session = use(FakeSession({(Label, 1): Label(label_id=1, label_name="A")}, scalar=3))
        resp = client.delete("/labels/1")
        assert resp.status_code == 409 and "3 wallet" in resp.json()["detail"]
        assert session.deleted == []

    def test_force_detaches_then_deletes(self, signed_in):
        client, _, use = signed_in
        label = Label(label_id=1, label_name="A")
        session = use(FakeSession({(Label, 1): label}, scalar=3))
        resp = client.delete("/labels/1", params={"force": "true"})
        assert resp.json() == {"status": "deleted", "label_id": 1, "detached": 3}
        assert session.deleted == [label]
        sql = " ".join(str(session.statements[-1].compile(dialect=PG)).split())
        assert sql.startswith("DELETE FROM wallet_label WHERE wallet_label.label_id =")

    def test_unattached_delete(self, signed_in):
        client, _, use = signed_in
        use(FakeSession({(Label, 1): Label(label_id=1, label_name="A")}, scalar=0))
        assert client.delete("/labels/1").json()["detached"] == 0

    def test_list_search_is_bound(self, signed_in):
        client, _, use = signed_in
        session = use(FakeSession(scalar=0))
        client.get("/labels", params={"q": "x'; DROP", "category": "risk"})
        statement = session.statements[-1]
        compiled = str(statement.compile(dialect=PG))
        assert "DROP" not in compiled and "label.category" in compiled and "ORDER BY label.label_id" in compiled


class TestWalletTags:
    def _session(self, **kw):
        return FakeSession({(Wallet, 5): Wallet(wallet_id=5), (Label, 1): Label(label_id=1, label_name="A"), **kw.get("extra", {})},
                           rows=[SimpleNamespace(_mapping={"label_id": 1, "label_name": "A"})])

    def test_records_who_tagged(self, signed_in):
        client, _, use = signed_in
        session = use(self._session())
        resp = client.post("/wallets/5/labels", json={"label_id": 1, "confidence": 0.9})
        assert resp.status_code == 201
        tag = session.added[0]
        assert (tag.wallet_id, tag.label_id, tag.tagged_by, tag.confidence) == (5, 1, 7, 0.9)

    @pytest.mark.parametrize("path, body", [("/wallets/6/labels", {"label_id": 1}), ("/wallets/5/labels", {"label_id": 2})])
    def test_unknown_wallet_or_label_is_404(self, signed_in, path, body):
        client, _, use = signed_in
        use(self._session())
        assert client.post(path, json=body).status_code == 404

    def test_already_tagged_is_409(self, signed_in):
        client, _, use = signed_in
        use(self._session(extra={(WalletLabel, (5, 1)): WalletLabel(wallet_id=5, label_id=1)}))
        assert client.post("/wallets/5/labels", json={"label_id": 1}).status_code == 409

    @pytest.mark.parametrize("confidence", [-0.1, 1.5])
    def test_confidence_range(self, signed_in, confidence):
        client, _, use = signed_in
        use(self._session())
        assert client.post("/wallets/5/labels", json={"label_id": 1, "confidence": confidence}).status_code == 422

    def test_remove(self, signed_in):
        client, _, use = signed_in
        tag = WalletLabel(wallet_id=5, label_id=1)
        session = use(FakeSession({(WalletLabel, (5, 1)): tag}))
        assert client.delete("/wallets/5/labels/1").status_code == 200 and session.deleted == [tag]
        assert client.delete("/wallets/5/labels/2").status_code == 404

    def test_wallet_body_shape_is_untouched(self):
        # Labels have their own endpoint; GET /wallets/{id} stays as it was.
        assert "labels" not in {c.key for c in api_main.WALLET_COLUMNS}


class TestAlertReview:
    def test_sets_status_and_reviewer(self, signed_in):
        client, _, use = signed_in
        alert = Alert(alert_id=3, run_id=1, pattern_type="SYBIL", severity="HIGH", review_status="NEW")
        use(FakeSession({(Alert, 3): alert}))
        resp = client.patch("/alerts/3", json={"review_status": "CONFIRMED"})
        assert resp.status_code == 200 and resp.json()["review_status"] == "CONFIRMED"
        assert (alert.review_status, alert.reviewed_by) == ("CONFIRMED", 7)

    def test_unknown_alert_and_bad_status(self, signed_in):
        client, _, use = signed_in
        use(FakeSession())
        assert client.patch("/alerts/3", json={"review_status": "CONFIRMED"}).status_code == 404
        assert client.patch("/alerts/3", json={"review_status": "REVIEWED"}).status_code == 422


@pytest.mark.parametrize("method, path, body", [
    ("post", "/labels", {"label_name": "x"}),
    ("patch", "/labels/1", {"category": "x"}),
    ("delete", "/labels/1", None),
    ("post", "/wallets/5/labels", {"label_id": 1}),
    ("delete", "/wallets/5/labels/1", None),
    ("patch", "/alerts/3", {"review_status": "NEW"}),
])
class TestAccess:
    def test_viewer_is_refused(self, signed_in, method, path, body):
        client, role, use = signed_in
        session = use(FakeSession())
        role["value"] = "viewer"
        resp = client.request(method.upper(), path, json=body)
        assert resp.status_code == 403 and session.added == [] and session.deleted == []

    def test_analyst_gets_past_the_gate(self, signed_in, method, path, body):
        client, _, use = signed_in
        use(FakeSession())
        assert client.request(method.upper(), path, json=body).status_code != 403

    def test_mock_mode_says_needs_live_mode(self, monkeypatch, method, path, body):
        monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
        monkeypatch.delenv("FRAMECHECK_PUBLIC_DEMO", raising=False)
        monkeypatch.setattr(api_main, "using_mock", lambda: True)
        resp = TestClient(api_main.app).request(method.upper(), path, json=body)
        assert resp.status_code == 409 and "live mode" in resp.json()["detail"]


def test_mock_mode_reads_work_without_a_database(monkeypatch):
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
    monkeypatch.setattr(api_main, "using_mock", lambda: True)
    client = TestClient(api_main.app)
    assert client.get("/labels").json() == [] and client.get("/wallets/1/labels").json() == []
