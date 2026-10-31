"""Paging on /wallets, /flags, /alerts -- offline, in mock mode. Bodies stay
arrays, metadata goes in headers, and without page/page_size the legacy
responses are unchanged."""
import os

import pytest
from fastapi.testclient import TestClient

os.environ["FRAMECHECK_API_MODE"] = "mock"
os.environ["FRAMECHECK_AUTH_MODE"] = "disabled"

from api import main as api_main  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_main, "using_mock", lambda: True)
    monkeypatch.setenv("FRAMECHECK_AUTH_MODE", "disabled")
    return TestClient(api_main.app)


@pytest.mark.parametrize("path", ["/wallets", "/flags", "/alerts"])
class TestPaging:
    def test_headers_and_array_body(self, client, path):
        resp = client.get(path, params={"page": 1, "page_size": 1})
        assert isinstance(resp.json(), list) and len(resp.json()) <= 1
        assert resp.headers["X-Page"] == "1" and resp.headers["X-Page-Size"] == "1"
        assert int(resp.headers["X-Total-Count"]) >= len(resp.json())

    def test_pages_cover_everything_once(self, client, path):
        total = int(client.get(path, params={"page": 1, "page_size": 1}).headers["X-Total-Count"])
        rows = [client.get(path, params={"page": n, "page_size": 1}).json() for n in range(1, total + 1)]
        assert [r[0] for r in rows] == client.get(path, params={"page": 1, "page_size": 500}).json()

    def test_bad_page_size_is_422(self, client, path):
        assert client.get(path, params={"page_size": 501}).status_code == 422
        assert client.get(path, params={"page": 0}).status_code == 422

    def test_no_page_params_means_no_page_headers(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200 and "X-Total-Count" not in resp.headers


def test_legacy_wallet_limit_is_unchanged(client):
    from api.main import load_mock
    assert client.get("/wallets", params={"limit": 2}).json() == load_mock()["wallets"][:2]


def test_wallet_filters_apply_when_paging(client):
    all_rows = client.get("/wallets", params={"page": 1, "page_size": 500}).json()
    address = all_rows[0]["address"]
    found = client.get("/wallets", params={"page": 1, "q": address[2:10].upper()}).json()
    assert address in [w["address"] for w in found]
    contract = client.get("/wallets", params={"page": 1, "address_type": "CONTRACT"}).json()
    assert all(w["address_type"] == "CONTRACT" for w in contract)
    assert client.get("/wallets", params={"page": 1, "sort": "nonsense"}).status_code == 422


def test_live_wallet_page_has_a_stable_order(monkeypatch, client):
    from unittest import mock

    from sqlalchemy.dialects import postgresql
    session = mock.MagicMock()
    session.scalar.return_value = 0
    session.execute.return_value = []
    session.__enter__.return_value = session
    monkeypatch.setattr(api_main, "using_mock", lambda: False)
    monkeypatch.setattr(api_main, "get_session", lambda: session)
    assert client.get("/wallets", params={"page": 2, "page_size": 10, "sort": "risk"}).status_code == 200
    sql = " ".join(str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect())).split())
    assert "ORDER BY wallet.risk_score DESC NULLS LAST, wallet.wallet_id" in sql
