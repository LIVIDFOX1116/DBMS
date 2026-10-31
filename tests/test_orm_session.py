"""db/orm/engine.py and db/orm/session.py -- offline, no Postgres.

The engine is never actually connected here: create_engine() is lazy about
connecting, and every test that would reach a database replaces the engine
or the session with a mock.
"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc

import api.main as api_main
from db.orm import engine as engine_module
from db.orm import session as session_module


@pytest.fixture(autouse=True)
def _fresh_engine():
    engine_module.reset_engine()
    yield
    engine_module.reset_engine()


def _sa_operational_error():
    return sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection"))


class TestGetSession:
    def _session(self, monkeypatch):
        session = mock.MagicMock()
        monkeypatch.setattr(session_module, "get_engine", lambda: "engine")
        monkeypatch.setattr(session_module, "Session", lambda *a, **k: session)
        return session

    def test_commits_and_closes_on_success(self, monkeypatch):
        session = self._session(monkeypatch)
        with session_module.get_session() as s:
            assert s is session
        session.commit.assert_called_once()
        session.rollback.assert_not_called()
        session.close.assert_called_once()

    def test_rolls_back_and_closes_on_an_ordinary_error(self, monkeypatch):
        session = self._session(monkeypatch)
        with pytest.raises(ValueError), session_module.get_session():
            raise ValueError("bug")
        session.rollback.assert_called_once()
        session.commit.assert_not_called()
        session.invalidate.assert_not_called()
        session.close.assert_called_once()

    def test_a_connection_level_error_discards_the_connection(self, monkeypatch):
        # The guarantee get_conn() gives its psycopg2 pool: a broken
        # connection is never handed to the next request.
        session = self._session(monkeypatch)
        with pytest.raises(sa_exc.OperationalError), session_module.get_session():
            raise _sa_operational_error()
        session.invalidate.assert_called_once()
        session.commit.assert_not_called()
        session.close.assert_called_once()


class TestEngine:
    def test_engine_is_lazy_and_built_once(self, monkeypatch):
        built = []
        monkeypatch.setattr(engine_module, "create_engine", lambda *a, **k: built.append(k) or mock.MagicMock())
        assert engine_module._engine is None, "importing the package must not build an engine"
        first = engine_module.get_engine()
        assert engine_module.get_engine() is first
        assert len(built) == 1
        assert built[0]["pool_pre_ping"] is True

    def test_connections_go_through_dbsetup_connect(self, monkeypatch):
        # dbsetup.connect() carries the IPv4-first loopback ordering; the
        # engine must use it rather than letting libpq resolve `localhost`.
        from api import dbsetup

        captured = {}
        monkeypatch.setattr(engine_module, "create_engine", lambda url, **k: captured.update(k) or mock.MagicMock())
        monkeypatch.setattr(dbsetup, "current_dsn", lambda: {"host": "localhost", "dbname": "x"})
        connect = mock.MagicMock(return_value="conn")
        monkeypatch.setattr(dbsetup, "connect", connect)
        engine_module.get_engine()
        assert captured["creator"]() == "conn"
        connect.assert_called_once_with({"host": "localhost", "dbname": "x"})

    def test_reset_disposes_and_forgets_the_engine(self, monkeypatch):
        engine = mock.MagicMock()
        monkeypatch.setattr(engine_module, "create_engine", lambda *a, **k: engine)
        engine_module.get_engine()
        engine_module.reset_engine()
        engine.dispose.assert_called_once()
        assert engine_module._engine is None

    def test_close_pool_also_resets_the_engine(self, monkeypatch):
        # close_pool() runs on /internal/db-settings, /db/mode and shutdown;
        # the ORM's pooled connections must not outlive the old settings.
        reset = mock.MagicMock()
        monkeypatch.setattr(api_main, "reset_engine", reset)
        api_main.close_pool()
        reset.assert_called_once()


class TestSqlalchemyErrorsAre503:
    def test_sqlalchemy_operational_error_is_database_unavailable(self, monkeypatch):
        monkeypatch.setattr(api_main, "using_mock", lambda: False)

        def _down():
            raise _sa_operational_error()

        monkeypatch.setattr(api_main, "get_conn", _down)
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.get("/wallets", headers={"Origin": "http://localhost:5500"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "database unavailable"}
        assert resp.headers["access-control-allow-origin"] == "http://localhost:5500"
