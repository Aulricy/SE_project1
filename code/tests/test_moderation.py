"""Moderation preserves history and rolls back every linked record on failure."""

import pytest
from sqlalchemy import event, select

from app.database import get_engine, get_session
from app.models import OperationLog, Product, ProductStatus, Reservation
from tests.helpers import post_form, token_for
from tests.product_helpers import product_action
from tests.reservation_helpers import (
    act, assert_consistent, book, new_key, reservation_id, state_snapshot,
)
from tests.test_reservation_races import _clones, _race


def moderate(client, product_id, version, operation="block", reason="违规描述，需要整改"):
    """Use the real CSRF boundary without requiring prior product-detail visibility."""
    return post_form(client, f"/products/{product_id}/actions/{operation}", {
        "version": version, "reason": reason,
    }, csrf_path="/account")


@pytest.mark.parametrize("state", ["DRAFT", "PENDING_REVIEW", "PUBLISHED", "OFF_SHELF"])
def test_block_without_active_reservation_and_restore(app, trade, state):
    """All eligible unoccupied states may be blocked; restore only produces a draft."""
    pid = trade["product_id"]
    with app.app_context():
        database = get_session()
        database.get(Product, pid).status = ProductStatus(state)
        database.commit()
    before = assert_consistent(app, pid)
    assert moderate(trade["admin"], pid, 2).status_code == 303
    blocked = assert_consistent(app, pid)
    assert blocked["product"][:2] == ("BLOCKED", 3)
    assert blocked["logs"] == before["logs"] + 1
    assert blocked["requests"] == before["requests"]
    assert app.test_client().get(f"/products/{pid}").status_code == 404
    assert "违规描述，需要整改" in trade["seller"].get(f"/products/{pid}").get_data(as_text=True)
    assert moderate(trade["seller"], pid, 3, "restore").status_code == 403
    assert moderate(trade["admin"], pid, 3, "restore").status_code == 303
    assert assert_consistent(app, pid)["product"][:2] == ("DRAFT", 4)
    assert product_action(trade["admin"], pid, 4, "approve").status_code == 409
    assert product_action(trade["seller"], pid, 4, "submit").status_code == 303
    assert product_action(trade["admin"], pid, 5, "approve").status_code == 303


@pytest.mark.parametrize("confirmed", [False, True])
def test_block_cancels_active_record_and_old_replay_never_revives_it(app, trade, confirmed):
    """A single composite audit event includes both transitions and the administrator reason."""
    pid = trade["product_id"]
    key = new_key()
    rid = reservation_id(book(trade["buyer"], pid, 2, key))
    if confirmed:
        assert act(trade["seller"], rid, "confirm", 1).status_code == 303
    version = 4 if confirmed else 3
    before = assert_consistent(app, pid)
    assert moderate(trade["admin"], pid, version).status_code == 303
    after = assert_consistent(app, pid)
    row = after["reservations"][0]
    assert after["product"][:2] == ("BLOCKED", version + 1)
    assert row[2:5] == ("CANCELLED", 3 if confirmed else 2, 9990)
    assert row[7] is not None and row[8] is None
    assert row[9:11] == (trade["ids"]["reviewer"], "违规描述，需要整改")
    assert after["requests"] == before["requests"]
    assert after["logs"] == before["logs"] + 1
    for actor in ("seller", "buyer"):
        html = trade[actor].get(f"/reservations/{rid}").get_data(as_text=True)
        assert "已屏蔽" in html and "已取消" in html and "违规描述，需要整改" in html
        assert "reviewer" in html and "确认预约</button>" not in html
    with app.app_context():
        log = get_session().scalar(select(OperationLog).order_by(OperationLog.id.desc()))
        assert log.operation_type == "BLOCK_PRODUCT"
        assert log.reservation_id == rid and log.reservation_status_after == "CANCELLED"
        assert log.product_status_before == ("CONFIRMED" if confirmed else "RESERVED")
        assert log.reservation_status_before == (
            "CONFIRMED" if confirmed else "PENDING_CONFIRMATION"
        )
    assert moderate(trade["admin"], pid, version + 1, "restore").status_code == 303
    assert assert_consistent(app, pid)["reservations"] == after["reservations"]
    assert product_action(trade["seller"], pid, version + 2, "submit").status_code == 303
    assert product_action(trade["admin"], pid, version + 3, "approve").status_code == 303
    new_rid = reservation_id(book(trade["buyer"], pid, version + 4))
    assert new_rid != rid
    final = assert_consistent(app, pid)
    assert reservation_id(book(trade["buyer"], pid, 2, key)) == rid
    assert assert_consistent(app, pid) == final
    assert act(trade["seller"], rid, "confirm", 1).status_code == 409
    assert act(trade["buyer"], rid, "cancel", 1, reason="旧请求").status_code == 409
    assert act(trade["buyer"], rid, "complete", 2).status_code == 409
    assert assert_consistent(app, pid) == final


@pytest.mark.parametrize("reason", ["", "  ", "长" * 1001])
def test_reason_is_required_and_preserved_without_changes(app, trade, reason):
    """Invalid moderation cannot bump versions or leave an audit event."""
    pid = trade["product_id"]
    before = assert_consistent(app, pid)
    response = moderate(trade["admin"], pid, 2, reason=reason)
    assert response.status_code == 400
    assert "已保留本次输入" in response.get_data(as_text=True)
    assert assert_consistent(app, pid) == before


def test_permissions_csrf_versions_and_completed_history(app, trade):
    """All entry points enforce role, POST, CSRF and current state on the server."""
    pid = trade["product_id"]
    before = assert_consistent(app, pid)
    path = f"/products/{pid}/actions/block"
    assert moderate(trade["seller"], pid, 2).status_code == 403
    assert moderate(trade["buyer"], pid, 2).status_code == 403
    assert trade["admin"].get(path).status_code == 405
    assert trade["admin"].post(path, data={"version": 2, "reason": "违规"}).status_code == 400
    assert moderate(trade["admin"], pid, 2, "restore").status_code == 409
    assert moderate(trade["admin"], pid, 1).status_code == 409
    assert assert_consistent(app, pid) == before
    rid = reservation_id(book(trade["buyer"], pid, 2))
    assert act(trade["seller"], rid, "confirm", 1).status_code == 303
    assert act(trade["buyer"], rid, "complete", 2).status_code == 303
    completed = assert_consistent(app, pid)
    assert moderate(trade["admin"], pid, 5).status_code == 409
    assert assert_consistent(app, pid) == completed


@pytest.mark.parametrize("stage", ["product", "reservation", "log", "commit"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_interruption_rolls_back_product_reservation_reason_and_log(app, trade, stage, confirmed):
    """Inject after the version claim and after flush; no partial success survives."""
    pid = trade["product_id"]
    rid = reservation_id(book(trade["buyer"], pid, 2))
    if confirmed:
        assert act(trade["seller"], rid, "confirm", 1).status_code == 303
    before = assert_consistent(app, pid)
    token = token_for(trade["admin"], "/account")
    with app.app_context():
        engine = get_engine()

    def interrupt(_conn, _cursor, statement, _params, _context, _many):
        prefix = {"product": "UPDATE products SET status", "reservation": "UPDATE reservations",
                  "log": "INSERT INTO operation_logs"}.get(stage)
        if prefix and statement.startswith(prefix):
            raise RuntimeError("Injected moderation interruption")

    def interrupt_commit(connection):
        # The business commit follows the CSRF/login read-only request setup.
        if stage == "commit":
            assert connection.exec_driver_sql(
                "SELECT status FROM products WHERE id=?", (pid,),
            ).scalar() == "BLOCKED"
            raise RuntimeError("Injected moderation interruption")

    event.listen(engine, "before_cursor_execute", interrupt)
    event.listen(engine, "commit", interrupt_commit)
    try:
        with pytest.raises(RuntimeError, match="Injected moderation"):
            trade["admin"].post(f"/products/{pid}/actions/block", data={
                "version": before["product"][1], "reason": "应当全部回滚的原因",
                "csrf_token": token,
            })
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
        event.remove(engine, "commit", interrupt_commit)
    assert assert_consistent(app, pid) == before
    with app.app_context():
        assert get_session().get(Product, pid).latest_block_reason is None
    assert moderate(trade["admin"], pid, before["product"][1]).status_code == 303


@pytest.mark.parametrize("operation", ["reserve", "confirm", "cancel", "complete"])
def test_block_racing_trade_transition_has_one_legal_winner(app, trade, operation):
    """The same product/record page versions cannot both advance in a moderation race."""
    pid = trade["product_id"]
    version, record_version, rid = 2, 1, None
    if operation != "reserve":
        rid = reservation_id(book(trade["buyer"], pid, 2))
        version = 3
    if operation == "complete":
        assert act(trade["seller"], rid, "confirm", 1).status_code == 303
        version, record_version = 4, 2
    actor = trade["seller"] if operation == "confirm" else trade["buyer"]
    path = (f"/products/{pid}/reserve" if operation == "reserve"
            else f"/reservations/{rid}/actions/{operation}")
    before = assert_consistent(app, pid)
    outcomes = _race([
        (trade["admin"], f"/products/{pid}/actions/block", {
            "version": version, "reason": "竞争屏蔽",
            "csrf_token": token_for(trade["admin"], "/account"),
        }),
        (actor, path, {"version": version if operation == "reserve" else record_version,
                       "reason": "竞争取消", "request_key": new_key(),
                       "csrf_token": token_for(actor, "/account")}),
    ])
    assert sorted(status for status, _location in outcomes) == [303, 409]
    after = assert_consistent(app, pid)
    assert after["product"][1] == version + 1
    assert after["logs"] == before["logs"] + 1
    if outcomes[0][0] == 303:
        assert after["product"][0] == "BLOCKED"
        assert after["requests"] == before["requests"]
    else:
        assert after["requests"] == before["requests"] + 1
    if rid is not None:
        assert after["reservations"][0][3] == record_version + 1


@pytest.mark.parametrize("operation", ["block", "restore"])
def test_duplicate_moderation_only_advances_once(app, trade, operation):
    """Ten simultaneous duplicate product-page submissions create one audit event."""
    pid, version = trade["product_id"], 2
    if operation == "restore":
        assert moderate(trade["admin"], pid, 2).status_code == 303
        version = 3
    before = assert_consistent(app, pid)
    clients, token = _clones(app, trade["admin"], 10)
    outcomes = _race([(client, f"/products/{pid}/actions/{operation}", {
        "version": version, "reason": "重复屏蔽", "csrf_token": token,
    }) for client in clients])
    assert sorted(status for status, _location in outcomes) == [303] + [409] * 9
    after = assert_consistent(app, pid)
    assert after["product"][1] == version + 1
    assert after["logs"] == before["logs"] + 1


def test_inconsistent_product_and_active_record_is_rejected_atomically(app, trade):
    """Even unexpected preexisting corruption cannot be hidden by moderation or restore."""
    pid = trade["product_id"]
    reservation_id(book(trade["buyer"], pid, 2))
    with app.app_context():
        database = get_session()
        database.get(Product, pid).status = ProductStatus.PUBLISHED
        database.commit()
    before = state_snapshot(app, pid)
    assert moderate(trade["admin"], pid, 3).status_code == 409
    assert state_snapshot(app, pid) == before


@pytest.mark.parametrize("phase", ["restore", "corrupt_restore"])
def test_restore_never_revives_or_silently_cancels_an_active_record(app, trade, phase):
    """Restoration with an interrupted log or an inconsistent active relation is all-or-none."""
    pid = trade["product_id"]
    rid = reservation_id(book(trade["buyer"], pid, 2))
    assert moderate(trade["admin"], pid, 3).status_code == 303
    if phase == "corrupt_restore":
        with app.app_context():
            database = get_session()
            record = database.get(Reservation, rid)
            record.status = "PENDING_CONFIRMATION"
            record.cancelled_at = record.cancelled_by = record.cancel_reason = None
            database.commit()
        before = state_snapshot(app, pid)
        assert moderate(trade["admin"], pid, 4, "restore").status_code == 409
        assert state_snapshot(app, pid) == before
        return
    before = assert_consistent(app, pid)
    token = token_for(trade["admin"], "/account")
    with app.app_context():
        engine = get_engine()

    def interrupt(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("INSERT INTO operation_logs"):
            raise RuntimeError("Injected restore log failure")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="Injected restore"):
            trade["admin"].post(f"/products/{pid}/actions/restore", data={
                "version": 4, "csrf_token": token,
            })
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    assert assert_consistent(app, pid) == before
