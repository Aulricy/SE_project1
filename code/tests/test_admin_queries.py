"""Beijing boundary accounting, combined audit filters and read-only access."""

from datetime import datetime, timedelta
from decimal import Decimal

from flask import g
import pytest
from sqlalchemy import select
from werkzeug.exceptions import Forbidden

from app.database import get_session
from app.models import OperationLog, OperationType, Product, ProductStatus, Reservation, User
from app.models.reservation import ReservationStatus
from app.services.admin_queries import LogQuery, LogService, StatisticsQuery, StatisticsService
from tests.helpers import login_as, post_form
from tests.reservation_helpers import act, book, reservation_id, state_snapshot
from tests.test_moderation import moderate


@pytest.fixture(name="stat_data")
def statistics_fixture(app, trade):
    """Generated trades straddle Beijing midnight and include fractional seconds."""
    times = [datetime(2026, 10, 30, 15, 59, 59, 999999),
             datetime(2026, 10, 30, 16),
             datetime(2026, 10, 31, 15, 59, 59, 999999), datetime(2026, 10, 31, 16)]
    with app.app_context():
        database = get_session()
        for number, timestamp in enumerate(times):
            product = Product(seller_id=trade["ids"]["member"], name=f"边界商品{number}",
                              status=ProductStatus.COMPLETED, price_cents=999999)
            database.add(product)
            database.flush()
            database.add(Reservation(
                buyer_id=trade["ids"]["other"], product_id=product.id,
                reserved_name=product.name, reserved_description="虚构统计边界数据",
                reserved_price_cents=[1, 1050, 2000, 3000][number],
                status=ReservationStatus.COMPLETED, confirmed_at=timestamp - timedelta(hours=1),
                completed_at=timestamp,
            ))
        database.commit()
    return trade


def _statistics(app, trade, start="2026-10-31", end="2026-10-31"):
    with app.app_context():
        g.user = get_session().get(User, trade["ids"]["reviewer"])
        return StatisticsService.get_completed_statistics(
            StatisticsQuery.from_mapping({"start": start, "end": end}),
        )


def test_statistics_beijing_boundaries_price_snapshots_and_empty(app, stat_data):
    """Count precisely both local endpoint days, using cents and the first completion timestamp."""
    result = _statistics(app, stat_data)
    assert result.completed_count == 2
    assert result.total_cents == 3050 and result.total_amount == Decimal("30.50")
    assert [row.reserved_price_cents for row in result.records.items] == [2000, 1050]
    assert _statistics(app, stat_data, "2026-10-30", "2026-11-01").total_cents == 6051
    empty = _statistics(app, stat_data, "2026-11-02", "2026-11-03")
    assert empty.completed_count == empty.total_cents == 0
    html = stat_data["admin"].get(
        "/admin/statistics?start=2026-10-31&end=2026-10-31",
    ).get_data(as_text=True)
    assert "¥30.50" in html and "边界商品1" in html and "边界商品2" in html
    assert "边界商品0" not in html and "边界商品3" not in html


@pytest.mark.parametrize("state", ["pending", "confirmed", "cancelled", "blocked", "completed"])
def test_only_first_successful_completion_counts(app, trade, state):
    """Unfinished and cancelled trades contribute zero; completion replay contributes once."""
    pid = trade["product_id"]
    rid = reservation_id(book(trade["buyer"], pid, 2))
    if state in {"confirmed", "completed"}:
        assert act(trade["seller"], rid, "confirm", 1).status_code == 303
    if state == "cancelled":
        assert act(trade["buyer"], rid, "cancel", 1, reason="取消测试").status_code == 303
    if state == "blocked":
        assert moderate(trade["admin"], pid, 3).status_code == 303
    if state == "completed":
        key = "repeated_completion_statistics"
        assert act(trade["buyer"], rid, "complete", 2, key).status_code == 303
        before = state_snapshot(app, pid)
        assert act(trade["buyer"], rid, "complete", 2, key).status_code == 303
        assert state_snapshot(app, pid) == before
    result = _statistics(app, trade, "2026-01-01", "2027-01-01")
    assert result.completed_count == (1 if state == "completed" else 0)
    assert result.total_cents == (9990 if state == "completed" else 0)


@pytest.mark.parametrize("query", [
    "start=2026-11-01&end=2026-10-31", "start=bad&end=2026-10-31",
    "start=2026-02-30&end=2026-10-31", "start=20261031&end=2026-10-31",
    "start=0001-01-01&end=2026-10-31", "start=2026-10-31&end=9999-12-31",
    "start=2026-10-31", "start=2026-10-31&end=2026-10-31&page=0",
])
def test_invalid_statistics_preserves_inputs_without_showing_fallback_totals(trade, query):
    """An invalid applied range must never display a different range's numeric result."""
    response = trade["admin"].get("/admin/statistics?" + query)
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'role="alert"' in html and 'id="completed-amount"' not in html


def test_combined_log_filters_boundary_seconds_and_full_detail(app, trade):
    """An admin can trace the linked cancellation by time, actor, both objects and event type."""
    pid = trade["product_id"]
    rid = reservation_id(book(trade["buyer"], pid, 2))
    reason = "<script>alert('不可执行')</script>\n完整处理原因"
    assert moderate(trade["admin"], pid, 3, reason=reason).status_code == 303
    with app.app_context():
        database = get_session()
        log = database.scalar(select(OperationLog).where(
            OperationLog.operation_type == OperationType.BLOCK_PRODUCT,
        ))
        log.operated_at = datetime(2026, 10, 30, 16, 0, 0, 999999)
        log_id = log.id
        database.commit()
    params = {"start": "2026-10-31T00:00:00", "end": "2026-10-31T00:00:00",
              "operator": "reviewer", "product_id": str(pid), "reservation_id": str(rid),
              "type": "BLOCK_PRODUCT"}
    before = state_snapshot(app, pid)
    assert f'/admin/logs/{log_id}' in trade["admin"].get(
        "/admin/logs", query_string=params,
    ).get_data(as_text=True)
    for name, value in (("operator", "member"), ("product_id", "99999"),
                        ("reservation_id", "99999"), ("type", "CANCEL_RESERVATION"),
                        ("start", "2026-10-31T00:00:01")):
        # A start later than end is validation error, otherwise empty combined result.
        result = trade["admin"].get("/admin/logs", query_string={**params, name: value})
        assert result.status_code == (400 if name == "start" else 200)
        assert f'/admin/logs/{log_id}' not in result.get_data(as_text=True)
    with app.app_context():
        g.user = get_session().get(User, trade["ids"]["reviewer"])
        page = LogService.query(LogQuery.from_mapping({
            **params, "operator": str(trade["ids"]["reviewer"]),
        }))
        assert page.total == 1 and page.items[0].log.id == log_id
    html = trade["admin"].get(f"/admin/logs/{log_id}").get_data(as_text=True)
    assert "待卖家确认 → 已取消" in html and "已预约 → 已屏蔽" in html
    assert "完整处理原因" in html and "&lt;script&gt;" in html
    assert "<script>alert" not in html and "password_hash" not in html and "request_key" not in html
    assert state_snapshot(app, pid) == before


@pytest.mark.parametrize("query", [
    "start=wrong", "end=2026-02-30T00:00:00", "start=2026-10-31T00:00:00%2B08:00",
    "start=0001-01-01T00:00:00", "type=DROP_TABLE", "product_id=-1",
    "product_id=9223372036854775808", "reservation_id=one", "page=100001",
    "start=2026-10-31T00:00:01&end=2026-10-31T00:00:00",
])
def test_log_query_validation(trade, query):
    """Malformed, overflow and reversed filters return controlled errors."""
    assert trade["admin"].get("/admin/logs?" + query).status_code == 400


@pytest.mark.parametrize("path", ["/admin/statistics", "/admin/logs", "/admin/logs/1"])
def test_admin_query_routes_and_service_authorization(app, trade, path):
    """Both administrators can query; ordinary users and anonymous visitors cannot."""
    assert trade["admin"].get(path).status_code == 200
    assert trade["buyer"].get(path).status_code == 403
    assert trade["seller"].get(path).status_code == 403
    assert app.test_client().get(path).status_code == 303
    initial = app.test_client()
    assert login_as(initial, "initial").status_code == 303
    assert initial.get(path).status_code == 200
    with app.app_context():
        g.user = get_session().get(User, trade["ids"]["member"])
        with pytest.raises(Forbidden):
            LogService.query(LogQuery())
        with pytest.raises(Forbidden):
            StatisticsService.get_completed_statistics(StatisticsQuery.from_mapping({
                "start": "2026-10-31", "end": "2026-10-31",
            }))


def test_logs_are_readonly_and_paginate_stably(app, trade):
    """Equal-time rows retain deterministic ordering and no HTTP mutations are available."""
    pid = trade["product_id"]
    with app.app_context():
        database = get_session()
        for number in range(23):
            database.add(OperationLog(
                operator_id=trade["ids"]["reviewer"], product_id=pid,
                operation_type=OperationType.BLOCK_PRODUCT, operated_at=datetime(2026, 1, 1),
                product_status_before=ProductStatus.PUBLISHED,
                product_status_after=ProductStatus.BLOCKED, reason=f"生成的分页日志{number}",
            ))
        database.commit()
        g.user = database.get(User, trade["ids"]["reviewer"])
        first = LogService.query(LogQuery(operation_type=OperationType.BLOCK_PRODUCT))
        second = LogService.query(LogQuery(operation_type=OperationType.BLOCK_PRODUCT, page=2))
        assert first.total == second.total == 23
        assert len(first.items) == 20 and len(second.items) == 3
        ids = [view.log.id for view in first.items + second.items]
        assert ids == sorted(set(ids), reverse=True)
    before = state_snapshot(app, pid)
    for path in ("/admin/logs", f"/admin/logs/{ids[0]}"):
        assert post_form(trade["admin"], path, {}, csrf_path="/account").status_code == 405
        assert trade["admin"].delete(path).status_code in {400, 405}
    html = trade["admin"].get("/admin/logs?type=BLOCK_PRODUCT").get_data(as_text=True)
    assert "type=BLOCK_PRODUCT&amp;page=2" in html
    for invalid in ("0", "999999", "9223372036854775808"):
        assert trade["admin"].get(f"/admin/logs/{invalid}").status_code == 404
    assert state_snapshot(app, pid) == before


def test_statistics_pagination_keeps_full_range_totals(app, stat_data):
    """The total is over the entire interval even when only twenty rows fit on one page."""
    with app.app_context():
        database = get_session()
        for number in range(21):
            product = Product(seller_id=stat_data["ids"]["member"], name=f"分页商品{number}",
                              status=ProductStatus.COMPLETED, price_cents=9999)
            database.add(product)
            database.flush()
            database.add(Reservation(
                buyer_id=stat_data["ids"]["other"], product_id=product.id,
                reserved_name=product.name, reserved_description="生成的分页统计数据",
                reserved_price_cents=50, status=ReservationStatus.COMPLETED,
                confirmed_at=datetime(2026, 10, 31), completed_at=datetime(2026, 10, 31, 1),
            ))
        database.commit()
        g.user = database.get(User, stat_data["ids"]["reviewer"])
        pages = [StatisticsService.get_completed_statistics(StatisticsQuery.from_mapping({
            "start": "2026-10-31", "end": "2026-10-31", "page": str(page),
        })) for page in (1, 2)]
        assert [len(page.records.items) for page in pages] == [20, 3]
        assert all(page.completed_count == 23 and page.total_cents == 4100 for page in pages)
        assert len({row.id for page in pages for row in page.records.items}) == 23
