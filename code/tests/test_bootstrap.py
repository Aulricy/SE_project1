"""Integration checks for startup, initialization, and database session lifecycle."""

from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app import create_app
from app.database import get_engine, get_session


def test_landing_and_database_health(app: Flask):
    """The initial routes and linked stylesheet are reachable."""
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "二手交易平台" in response.get_data(as_text=True)
    assert client.get("/static/css/main.css").status_code == 200
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}


def test_init_db_is_repeatable_and_preserves_existing_data(app: Flask):
    """Initialization must not erase data when it is run again."""
    runner = app.test_cli_runner()
    assert runner.invoke(args=["init-db"]).exit_code == 0
    with app.app_context():
        engine = get_engine()
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE bootstrap_probe (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO bootstrap_probe (id) VALUES (1)"))
        assert "bootstrap_probe" in inspect(engine).get_table_names()
    assert runner.invoke(args=["init-db"]).exit_code == 0
    with app.app_context():
        assert get_session().scalar(text("SELECT COUNT(*) FROM bootstrap_probe")) == 1


def test_uncommitted_work_is_rolled_back(app: Flask):
    """A request-scoped session must never silently commit incomplete work."""
    with app.app_context():
        with get_engine().begin() as connection:
            connection.execute(text("CREATE TABLE rollback_probe (id INTEGER PRIMARY KEY)"))
        get_session().execute(text("INSERT INTO rollback_probe (id) VALUES (1)"))
    with app.app_context():
        assert get_session().scalar(text("SELECT COUNT(*) FROM rollback_probe")) == 0
        assert get_session().scalar(text("PRAGMA foreign_keys")) == 1


def test_health_reports_database_failure(app: Flask, monkeypatch):
    """A connection failure returns a failing readiness response without database details."""
    engine = app.extensions["database_engine"]
    engine.dispose()

    def fail_connection(*_args, **_kwargs):
        """Simulate an unavailable database file."""
        raise OperationalError("SELECT 1", {}, Exception("private database detail"))

    monkeypatch.setattr(engine, "connect", fail_connection)
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "error", "database": "unavailable"}
    assert b"private database detail" not in response.data


def test_secret_key_is_required(tmp_path: Path):
    """Missing local configuration fails clearly before the app starts."""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({
            "SECRET_KEY": "",
            "DATABASE_URL": f"sqlite:///{(tmp_path / 'unused.sqlite3').as_posix()}",
        })
