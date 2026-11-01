"""Legacy log migration preserves actual rows and rolls back interrupted DDL."""

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.schema import CreateTable

from app.models import OperationLog

from app.database import get_engine
from tests.reservation_helpers import book, reservation_id, state_snapshot

_LEGACY_COLUMNS = (
    "id, operator_id, product_id, operation_type, operated_at, "
    "product_status_before, product_status_after, reason"
)
_LEGACY_DDL = """
CREATE TABLE operation_logs_legacy (
    id INTEGER NOT NULL PRIMARY KEY,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    operation_type VARCHAR(20) NOT NULL CHECK(operation_type IN (
        'SUBMIT_REVIEW','WITHDRAW_REVIEW','APPROVE_REVIEW',
        'REJECT_REVIEW','TAKE_DOWN','RETURN_TO_DRAFT'
    )),
    operated_at DATETIME NOT NULL,
    product_status_before VARCHAR(14) NOT NULL,
    product_status_after VARCHAR(14) NOT NULL,
    reason TEXT
)
"""


def _legacy_database(app):
    """Reconstruct the previous module's real schema while preserving its product logs."""
    with app.app_context():
        engine = get_engine()
        with engine.begin() as connection:
            original = connection.exec_driver_sql(
                f"SELECT {_LEGACY_COLUMNS} FROM operation_logs ORDER BY id",
            ).all()
            connection.exec_driver_sql(_LEGACY_DDL)
            connection.exec_driver_sql(
                f"INSERT INTO operation_logs_legacy ({_LEGACY_COLUMNS}) "
                f"SELECT {_LEGACY_COLUMNS} FROM operation_logs",
            )
            connection.exec_driver_sql("DROP TABLE operation_logs")
            connection.exec_driver_sql("ALTER TABLE operation_logs_legacy RENAME TO operation_logs")
            connection.exec_driver_sql(
                "CREATE INDEX ix_log_product_time ON operation_logs(product_id, operated_at)",
            )
            connection.exec_driver_sql("DROP TABLE operation_requests")
            connection.exec_driver_sql("DROP TABLE reservations")
        return engine, original


def test_upgrade_preserves_old_rows_and_supports_new_reservation_logs(app, trade):
    """Re-running init-db upgrades only the legacy log table and enables atomic new bookings."""
    engine, original = _legacy_database(app)
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.exception
    with engine.connect() as connection:
        copied = connection.exec_driver_sql(
            f"SELECT {_LEGACY_COLUMNS} FROM operation_logs ORDER BY id",
        ).all()
        assert copied == original
        assert "reservation_id" in {
            column["name"] for column in inspect(connection).get_columns("operation_logs")
        }
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    record_id = reservation_id(book(trade["buyer"], trade["product_id"], 2))
    assert record_id > 0
    before = state_snapshot(app, trade["product_id"])
    assert app.test_cli_runner().invoke(args=["init-db"]).exit_code == 0
    assert state_snapshot(app, trade["product_id"]) == before


def test_interrupted_upgrade_restores_the_original_log_table(app, trade):
    """A failure after copy/drop but before rename must roll back all DDL and preserve rows."""
    assert trade
    engine, original = _legacy_database(app)

    def interrupt(_connection, _cursor, statement, _parameters, _context, _executemany):
        """Inject a failure immediately before the replacement table is renamed."""
        if statement.startswith("ALTER TABLE operation_logs_upgrade"):
            raise RuntimeError("Injected schema interruption")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        result = app.test_cli_runner().invoke(args=["init-db"])
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            f"SELECT {_LEGACY_COLUMNS} FROM operation_logs ORDER BY id",
        ).all() == original
        assert "reservation_id" not in {
            column["name"] for column in inspect(connection).get_columns("operation_logs")
        }
        assert "operation_logs_upgrade" not in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT COUNT(*) FROM operation_logs")) == len(original)


@pytest.mark.parametrize("interrupted", [False, True])
def test_upgrade_from_reservation_stage_preserves_all_tables(app, trade, interrupted):
    """The ten-event schema already has reservation fields; none may be lost during expansion."""
    pid = trade["product_id"]
    reservation_id(book(trade["buyer"], pid, 2))
    with app.app_context():
        engine = get_engine()
        with engine.begin() as connection:
            ddl = str(CreateTable(OperationLog.__table__).compile(dialect=engine.dialect))
            ddl = ddl.replace("CREATE TABLE operation_logs", "CREATE TABLE old_logs", 1)
            ddl = ddl.replace(", 'BLOCK_PRODUCT', 'RESTORE_TO_DRAFT'", "")
            assert "BLOCK_PRODUCT" not in ddl
            connection.exec_driver_sql(ddl)
            connection.exec_driver_sql("INSERT INTO old_logs SELECT * FROM operation_logs")
            connection.exec_driver_sql("DROP TABLE operation_logs")
            connection.exec_driver_sql("ALTER TABLE old_logs RENAME TO operation_logs")
            connection.exec_driver_sql(
                "CREATE INDEX ix_log_product_time ON operation_logs(product_id, operated_at)",
            )
            tables = ("users", "products", "reservations", "operation_requests", "operation_logs")
            original = {name: connection.exec_driver_sql(
                f"SELECT * FROM {name} ORDER BY id",
            ).all() for name in tables}
    before = state_snapshot(app, pid)

    def interrupt(_connection, _cursor, statement, _parameters, _context, _executemany):
        if interrupted and statement.startswith("ALTER TABLE operation_logs_upgrade"):
            raise RuntimeError("Injected reservation-stage upgrade interruption")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        result = app.test_cli_runner().invoke(args=["init-db"])
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    assert (result.exit_code != 0) == interrupted
    with engine.connect() as connection:
        for name in tables:
            assert connection.exec_driver_sql(
                f"SELECT * FROM {name} ORDER BY id",
            ).all() == original[name]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert "operation_logs_upgrade" not in inspect(connection).get_table_names()
    assert state_snapshot(app, pid) == before
    if interrupted:
        assert app.test_cli_runner().invoke(args=["init-db"]).exit_code == 0
    # New moderation events must fit the upgraded CHECK, including the linked reservation.
    from tests.test_moderation import moderate  # pylint: disable=import-outside-toplevel
    assert moderate(trade["admin"], pid, 3).status_code == 303
