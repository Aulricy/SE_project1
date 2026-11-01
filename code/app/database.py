"""Database engine setup, request-scoped sessions, and initialization."""

import sqlite3

import click
from flask import Flask, current_app, g
from flask.cli import with_appcontext
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session

from app.models import Base
from app.schema import upgrade_operation_logs


def _configure_sqlite(connection: sqlite3.Connection, _record) -> None:
    """Enable foreign keys outside a transaction before restoring transaction mode."""
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
    finally:
        connection.autocommit = previous_autocommit


def init_app(app: Flask) -> None:
    """Attach a separate engine to each app; business services own their commits."""
    database_url = app.config["DATABASE_URL"]
    if str(database_url).startswith("sqlite"):
        engine = create_engine(database_url, connect_args={"autocommit": False})
        event.listen(engine, "connect", _configure_sqlite)
    else:
        engine = create_engine(database_url)
    app.extensions["database_engine"] = engine
    app.teardown_appcontext(close_session)
    app.cli.add_command(init_db_command)


def get_engine() -> Engine:
    """Return the engine associated with the active application."""
    return current_app.extensions["database_engine"]


def get_session() -> Session:
    """Lazily open a session for the active request or application context."""
    if "database_session" not in g:
        g.database_session = Session(get_engine(), expire_on_commit=False)
    return g.database_session


def close_session(_error: BaseException | None = None) -> None:
    """Close the session and roll back any transaction that was not committed."""
    session = g.pop("database_session", None)
    if session is not None:
        session.close()


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Create registered tables without dropping or clearing existing data."""
    engine = get_engine()
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        upgrade_operation_logs(connection)
        if engine.dialect.name == "sqlite":
            initial_id = current_app.config["INITIAL_ADMIN_ID"]
            sequence = connection.scalar(text("SELECT seq FROM sqlite_sequence WHERE name='users'"))
            if sequence is None:
                connection.execute(
                    text("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', :initial_id)"),
                    {"initial_id": initial_id},
                )
            elif sequence < initial_id:
                connection.execute(
                    text("UPDATE sqlite_sequence SET seq=:initial_id WHERE name='users'"),
                    {"initial_id": initial_id},
                )
        connection.execute(text("SELECT 1"))
    click.echo("数据库初始化完成，已保留首个管理员编号；已有账号和数据保持不变。")
