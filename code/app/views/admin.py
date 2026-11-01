"""Administrator-only read pages for transaction statistics and operation logs."""

from datetime import datetime

from flask import Blueprint, render_template, request, url_for

from app.models import OperationType
from app.services.admin_queries import (
    AdminQueryError, BEIJING, LogQuery, LogService, StatisticsQuery, StatisticsService,
)
from app.services.users import UserService

blueprint = Blueprint("admin", __name__)


def _pagination(endpoint, page, values):
    params = {key: value for key, value in values.items() if key != "page"}
    return {
        "previous_url": url_for(endpoint, **params, page=page.page - 1) if page.page > 1 else None,
        "next_url": (url_for(endpoint, **params, page=page.page + 1)
                     if page.page < page.pages else None),
    }


@blueprint.get("/admin/statistics")
def statistics():
    """Default to the current Beijing month; retain invalid input with no misleading total."""
    UserService.require_admin()
    today = datetime.now(BEIJING).date()
    values = request.args.to_dict() if request.args else {
        "start": today.replace(day=1).isoformat(), "end": today.isoformat(),
    }
    try:
        query = StatisticsQuery.from_mapping(values)
        result = StatisticsService.get_completed_statistics(query)
    except AdminQueryError as error:
        return render_template("admin/statistics.html", values=values, result=None,
                               query_error=str(error)), 400
    return render_template("admin/statistics.html", values=values, result=result,
                           **_pagination("admin.statistics", result.records, values))


@blueprint.get("/admin/logs")
def logs():
    """Filter logs by Beijing seconds, exact actor, objects and operation type."""
    UserService.require_admin()
    values = request.args.to_dict()
    context = {"values": values, "operation_types": list(OperationType)}
    try:
        page = LogService.query(LogQuery.from_mapping(values))
    except AdminQueryError as error:
        return render_template("admin/logs.html", page=None, query_error=str(error), **context), 400
    return render_template("admin/logs.html", page=page, **context,
                           **_pagination("admin.logs", page, values))


@blueprint.get("/admin/logs/<int:log_id>")
def log_detail(log_id: int):
    """Show full reasons and both object state transitions without truncation."""
    return render_template("admin/log_detail.html", record=LogService.get_detail(log_id))


def init_app(app):
    """Attach the read-only administrator blueprint."""
    app.register_blueprint(blueprint)
