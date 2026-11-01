"""Transactional SQLite upgrades preserving product and reservation audit history."""

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.operation_log import OperationLog

_LEGACY_LOG_COLUMNS = {
    "id", "operator_id", "product_id", "operation_type", "operated_at",
    "product_status_before", "product_status_after", "reason",
}


def upgrade_operation_logs(connection) -> None:
    """Preserve all product logs while extending their enum constraint and reservation fields."""
    columns = {column["name"] for column in inspect(connection).get_columns("operation_logs")}
    checks = inspect(connection).get_check_constraints("operation_logs")
    if columns == set(OperationLog.__table__.columns.keys()) and any(
        "BLOCK_PRODUCT" in check["sqltext"] and "RESTORE_TO_DRAFT" in check["sqltext"]
        for check in checks
    ):
        return
    supported = (columns == _LEGACY_LOG_COLUMNS
                 or columns == set(OperationLog.__table__.columns.keys()))
    if connection.dialect.name != "sqlite" or not supported:
        raise RuntimeError("操作日志表结构不符合已支持的升级版本，未改写原表。")
    if inspect(connection).get_indexes("operation_logs") != [{
        "name": "ix_log_product_time", "column_names": ["product_id", "operated_at"],
        "unique": 0, "dialect_options": {},
    }]:
        raise RuntimeError("操作日志存在额外索引，请先核对结构；未改写原表。")
    if connection.scalar(text(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='operation_logs'"
    )):
        raise RuntimeError("操作日志存在自定义触发器，未改写原表。")
    before = connection.scalar(text("SELECT count(*) FROM operation_logs"))
    table = OperationLog.__table__
    ddl = str(CreateTable(table).compile(dialect=connection.dialect)).replace(
        "CREATE TABLE operation_logs", "CREATE TABLE operation_logs_upgrade", 1,
    )
    connection.exec_driver_sql(ddl)
    names = [column.name for column in table.columns if column.name in columns]
    fields = ", ".join(names)
    connection.exec_driver_sql(
        f"INSERT INTO operation_logs_upgrade ({fields}) SELECT {fields} FROM operation_logs",
    )
    copied = connection.scalar(text("SELECT count(*) FROM operation_logs_upgrade"))
    if copied != before:
        raise RuntimeError("日志复制数量不一致，升级已回滚。")
    connection.exec_driver_sql("DROP TABLE operation_logs")
    connection.exec_driver_sql("ALTER TABLE operation_logs_upgrade RENAME TO operation_logs")
    for index in table.indexes:
        connection.execute(CreateIndex(index))
