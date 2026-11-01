"""Reservation lifecycle, authorization, history, idempotency, and rollback checks."""

from datetime import timedelta
from io import BytesIO

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import create_app
from app.database import get_session
from app.models import (
    OperationLog, OperationRequestRecord, Product, ProductStatus, Reservation, ReservationStatus,
)
from app.models.base import utc_now
from tests.helpers import login_as, post_form, token_for
from tests.product_helpers import VALID_PRODUCT, create_product, png_bytes, product_action
from tests.reservation_helpers import (
    act, assert_consistent, book, command_context, new_key, reservation_id, state_snapshot,
)


def test_booking_confirmation_completion_and_replay(app, trade):
    """Three accepted commands atomically finish the trade, and replay changes nothing."""
    product_id = trade["product_id"]
    reserve_key, confirm_key, complete_key = new_key(), new_key(), new_key()
    record_id = reservation_id(book(
        trade["buyer"], product_id, 2, reserve_key,
        buyer_id=trade["ids"]["member"], reserved_price="0.01",
    ))
    first = assert_consistent(app, product_id)
    assert first["product"][:2] == (ProductStatus.RESERVED, 3)
    assert first["reservations"][0][1] == trade["ids"]["other"]
    assert first["reservations"][0][4] == 9990
    assert act(trade["seller"], record_id, "confirm", 1, confirm_key).status_code == 303
    second = assert_consistent(app, product_id)
    assert second["product"][:2] == (ProductStatus.CONFIRMED, 4)
    assert act(trade["buyer"], record_id, "complete", 2, complete_key).status_code == 303
    final = assert_consistent(app, product_id)
    assert final["product"][:2] == (ProductStatus.COMPLETED, 5)
    assert final["reservations"][0][2:5] == (ReservationStatus.COMPLETED, 3, 9990)
    assert final["reservations"][0][8] is not None
    assert final["requests"] == 3 and final["logs"] == 5
    for client, operation, version, key in (
        (trade["buyer"], "reserve", 2, reserve_key),
        (trade["seller"], "confirm", 1, confirm_key),
        (trade["buyer"], "complete", 2, complete_key),
    ):
        response = (
            book(client, product_id, version, key) if operation == "reserve"
            else act(client, record_id, operation, version, key)
        )
        assert reservation_id(response) == record_id
        assert state_snapshot(app, product_id) == final
    assert act(trade["buyer"], record_id, "complete", 3).status_code == 409
    assert act(trade["seller"], record_id, "cancel", 3, reason="已完成后取消").status_code == 409
    assert state_snapshot(app, product_id) == final


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("actor", ["buyer", "seller"])
def test_either_participant_can_cancel_with_reason(app, trade, confirmed, actor):
    """Pending rejection and confirmed cancellation retain reasons and release the product."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    if confirmed:
        assert act(trade["seller"], record_id, "confirm", 1).status_code == 303
    version = 2 if confirmed else 1
    key = new_key()
    response = act(trade[actor], record_id, "cancel", version, key, reason="  时间不合适  ")
    assert response.status_code == 303
    final = assert_consistent(app, product_id)
    row = final["reservations"][0]
    assert final["product"][0] == ProductStatus.PUBLISHED
    assert row[2] == ReservationStatus.CANCELLED and row[7] is not None
    assert row[9] == trade["ids"]["other" if actor == "buyer" else "member"]
    assert row[10] == "时间不合适"
    response = act(trade[actor], record_id, "cancel", version, key, reason="  时间不合适  ")
    assert response.status_code == 303
    assert state_snapshot(app, product_id) == final
    for client in (trade["buyer"], trade["seller"]):
        html = client.get(f"/reservations/{record_id}").get_data(as_text=True)
        assert "时间不合适" in html and "¥99.90" in html


def test_old_confirmation_and_old_cancel_cannot_cross_versions(app, trade):
    """Old confirmation/cancellation pages cannot cross a successful state transition."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    assert act(trade["seller"], record_id, "confirm", 1).status_code == 303
    before = state_snapshot(app, product_id)
    key = new_key()
    response = act(trade["buyer"], record_id, "cancel", 1, key, reason="保留的取消原因")
    assert response.status_code == 409
    html = response.get_data(as_text=True)
    assert "保留的取消原因" in html and f'value="{key}"' in html
    assert 'name="version" value="1"' in html
    assert state_snapshot(app, product_id) == before
    assert act(trade["buyer"], record_id, "cancel", 2, reason="刷新后取消").status_code == 303
    cancelled = state_snapshot(app, product_id)
    assert act(trade["seller"], record_id, "confirm", 1).status_code == 409
    assert state_snapshot(app, product_id) == cancelled


@pytest.mark.parametrize("reason", ["", "   ", "原" * 1001])
def test_invalid_cancel_keeps_original_input_and_state(app, trade, reason):
    """Invalid cancellations neither consume a success key nor change the occupied item."""
    record_id = reservation_id(book(trade["buyer"], trade["product_id"], 2))
    before = state_snapshot(app, trade["product_id"])
    key = new_key()
    response = act(trade["buyer"], record_id, "cancel", 1, key, reason=reason)
    assert response.status_code == 400
    assert f'value="{key}"' in response.get_data(as_text=True)
    assert state_snapshot(app, trade["product_id"]) == before


def test_permissions_and_own_item_booking(app, trade):
    """Identity fields cannot grant buyer/seller permissions and unrelated records stay private."""
    product_id = trade["product_id"]
    before = state_snapshot(app, product_id)
    assert book(trade["seller"], product_id, 2).status_code == 400
    assert book(trade["admin"], product_id, 2).status_code == 403
    guest = app.test_client()
    assert post_form(guest, f"/products/{product_id}/reserve", {
        "version": 2, "request_key": new_key(),
    }, csrf_path="/auth/login").status_code == 303
    assert state_snapshot(app, product_id) == before
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    before = state_snapshot(app, product_id)
    response = act(trade["buyer"], record_id, "confirm", 1, actor=trade["ids"]["member"])
    assert response.status_code == 403
    assert act(trade["seller"], record_id, "complete", 1).status_code == 403
    assert act(trade["admin"], record_id, "cancel", 1, reason="管理员冒充").status_code == 403
    stranger = app.test_client()
    assert login_as(stranger, "outsider").status_code == 303
    assert stranger.get(f"/reservations/{record_id}").status_code == 404
    assert act(stranger, record_id, "cancel", 1, reason="无关账号").status_code == 403
    assert state_snapshot(app, product_id) == before
    assert trade["seller"].get(f"/products/{product_id}/edit").status_code == 409
    assert product_action(trade["seller"], product_id, 3, "take-down").status_code == 409


def test_rebooking_and_old_replays_preserve_history_and_current_occupant(app, trade):
    """Cancelled requests never revive a booking or overwrite a newer price/occupant."""
    product_id = trade["product_id"]
    first_key, cancel_key = new_key(), new_key()
    old_id = reservation_id(book(trade["buyer"], product_id, 2, first_key))
    assert act(trade["buyer"], old_id, "cancel", 1, cancel_key, reason="重新考虑").status_code == 303
    assert product_action(trade["seller"], product_id, 4, "return-draft").status_code == 303
    assert post_form(trade["seller"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "price": "20.50", "version": 5, "intent": "submit",
    }).status_code == 303
    assert product_action(trade["admin"], product_id, 6, "approve").status_code == 303
    new_id = reservation_id(book(trade["buyer"], product_id, 7))
    final = assert_consistent(app, product_id)
    assert old_id != new_id
    assert final["reservations"][0][2] == ReservationStatus.CANCELLED
    assert final["reservations"][0][4] == 9990
    assert final["reservations"][1][4] == 2050
    assert reservation_id(book(trade["buyer"], product_id, 2, first_key)) == old_id
    replay = act(trade["buyer"], old_id, "cancel", 1, cancel_key, reason="重新考虑")
    assert reservation_id(replay) == old_id
    assert state_snapshot(app, product_id) == final
    assert book(trade["buyer"], product_id, 2).status_code == 409


def test_request_key_content_conflicts_and_failed_keys_are_not_consumed(app, trade):
    """Only success persists a key; changed version, operation, target or reason then conflicts."""
    product_id = trade["product_id"]
    key = new_key()
    assert book(trade["buyer"], product_id, 1, key).status_code == 409
    record_id = reservation_id(book(trade["buyer"], product_id, 2, key))
    before = state_snapshot(app, product_id)
    assert book(trade["buyer"], product_id, 3, key).status_code == 409
    assert book(trade["buyer"], 99999, 2, key).status_code == 409
    assert act(trade["buyer"], record_id, "complete", 1, key).status_code == 409
    assert state_snapshot(app, product_id) == before
    cancel_key = new_key()
    assert act(trade["buyer"], record_id, "cancel", 1, cancel_key).status_code == 400
    assert act(trade["buyer"], record_id, "cancel", 1, cancel_key, reason="原因 A").status_code == 303
    cancelled = state_snapshot(app, product_id)
    assert act(trade["buyer"], record_id, "cancel", 1, cancel_key, reason="原因 B").status_code == 409
    assert state_snapshot(app, product_id) == cancelled


@pytest.mark.parametrize("stage", ["product", "reservation", "request", "log"])
def test_new_booking_failure_rolls_back_all_four_records(app, trade, stage):
    """Failure at each persistence stage leaves no booking, occupation, success key or log."""
    product_id = trade["product_id"]
    before = state_snapshot(app, product_id)
    key = new_key()
    token = token_for(trade["buyer"], "/account")

    def fail_flush(database, _context, _instances):
        """Inject an interruption at the chosen flush stage."""
        classes = {
            "product": Product, "reservation": Reservation,
            "request": OperationRequestRecord, "log": OperationLog,
        }
        if any(isinstance(record, classes[stage]) for record in database.new | database.dirty):
            raise RuntimeError("Injected reservation persistence failure")

    event.listen(Session, "before_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="Injected reservation"):
            trade["buyer"].post(f"/products/{product_id}/reserve", data={
                "version": 2, "request_key": key, "csrf_token": token,
            })
    finally:
        event.remove(Session, "before_flush", fail_flush)
    assert state_snapshot(app, product_id) == before
    assert book(trade["buyer"], product_id, 2, key).status_code == 303
    assert_consistent(app, product_id)


@pytest.mark.parametrize("operation", ["confirm", "cancel", "complete"])
def test_trade_transition_log_failure_rolls_back_both_states(app, trade, operation):
    """Confirm, cancel and complete each roll back their two states, versions and success key."""
    product_id, record_id, version, client = command_context(trade, operation)
    before = state_snapshot(app, product_id)
    token = token_for(client, "/account")

    def fail_log(database, _context, _instances):
        """Fail the log write before a transaction can commit."""
        if any(isinstance(record, OperationLog) for record in database.new):
            raise RuntimeError("Injected trade log failure")

    event.listen(Session, "before_flush", fail_log)
    try:
        with pytest.raises(RuntimeError, match="Injected trade"):
            client.post(f"/reservations/{record_id}/actions/{operation}", data={
                "version": version, "request_key": new_key(), "reason": "取消原因",
                "csrf_token": token,
            })
    finally:
        event.remove(Session, "before_flush", fail_log)
    assert state_snapshot(app, product_id) == before


@pytest.mark.parametrize("confirmed", [False, True])
def test_active_uniqueness_is_a_database_constraint(app, trade, confirmed):
    """Direct inserts cannot bypass the one-active-reservation invariant."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    if confirmed:
        assert act(trade["seller"], record_id, "confirm", 1).status_code == 303
    with app.app_context():
        database = get_session()
        database.add(Reservation(
            buyer_id=trade["ids"]["member"], product_id=product_id,
            reserved_price_cents=9990, status=ReservationStatus.PENDING_CONFIRMATION,
            reserved_name=VALID_PRODUCT["name"], reserved_description=VALID_PRODUCT["description"],
        ))
        with pytest.raises(IntegrityError):
            database.commit()
        database.rollback()
    assert_consistent(app, product_id)


def test_private_description_image_and_persistent_replay(app, trade):
    """The related buyer retains access after public removal, including after app restart."""
    image_product = create_product(
        trade["seller"], intent="submit", image=(BytesIO(png_bytes()), "fixture.png"),
    )
    assert product_action(trade["admin"], image_product, 1, "approve").status_code == 303
    key = new_key()
    record_id = reservation_id(book(trade["buyer"], image_product, 2, key))
    assert app.test_client().get(f"/products/{image_product}").status_code == 404
    assert trade["buyer"].get(f"/products/{image_product}/image").status_code == 404
    assert trade["buyer"].get(f"/reservations/{record_id}/image").status_code == 200
    assert VALID_PRODUCT["description"] in trade["buyer"].get(
        f"/reservations/{record_id}",
    ).get_data(as_text=True)
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": app.config["SECRET_KEY"],
        "DATABASE_URL": app.config["DATABASE_URL"], "UPLOAD_FOLDER": app.config["UPLOAD_FOLDER"],
    })
    before = state_snapshot(app, image_product)
    try:
        client = restarted.test_client()
        cookie = trade["buyer"].get_cookie(app.config["SESSION_COOKIE_NAME"])
        client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie.value)
        assert reservation_id(book(client, image_product, 2, key)) == record_id
        assert client.get(f"/reservations/{record_id}/image").status_code == 200
    finally:
        restarted.extensions["database_engine"].dispose()
    assert state_snapshot(app, image_product) == before


def test_csrf_wrong_states_and_no_automatic_timeout(app, trade):
    """CSRF, unconfirmed completion and obsolete product pages cannot mutate business data."""
    product_id = trade["product_id"]
    before = state_snapshot(app, product_id)
    assert trade["buyer"].get(f"/products/{product_id}/reserve").status_code == 405
    assert trade["buyer"].post(f"/products/{product_id}/reserve", data={
        "version": 2, "request_key": new_key(),
    }).status_code == 400
    assert state_snapshot(app, product_id) == before
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    before = state_snapshot(app, product_id)
    assert act(trade["buyer"], record_id, "complete", 1).status_code == 409
    with app.app_context():
        database = get_session()
        database.get(Reservation, record_id).created_at = utc_now() - timedelta(days=30)
        database.commit()
    assert trade["buyer"].get(f"/reservations/{record_id}").status_code == 200
    current = state_snapshot(app, product_id)["reservations"][0][2]
    assert current == ReservationStatus.PENDING_CONFIRMATION
    assert before["requests"] == state_snapshot(app, product_id)["requests"]


def test_inconsistent_data_is_rejected_instead_of_overwritten(app, trade):
    """A deliberately inconsistent fixture must not be silently advanced or taken down."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    with app.app_context():
        database = get_session()
        database.get(Product, product_id).status = ProductStatus.PUBLISHED
        database.commit()
    before = state_snapshot(app, product_id)
    assert act(trade["seller"], record_id, "confirm", 1).status_code == 409
    assert product_action(trade["seller"], product_id, 3, "take-down").status_code == 409
    assert state_snapshot(app, product_id) == before


def test_history_filters_never_accept_someone_elses_identity(app, trade):
    """Buying/selling history remains participant-scoped, including after cancellation."""
    record_id = reservation_id(book(trade["buyer"], trade["product_id"], 2))
    assert act(trade["buyer"], record_id, "cancel", 1, reason="历史保留").status_code == 303
    buyer_html = trade["buyer"].get(
        "/reservations?side=buying&status=CANCELLED&buyer_id=1",
    ).get_data(as_text=True)
    seller_html = trade["seller"].get("/reservations?side=selling").get_data(as_text=True)
    assert "共 1 条买入记录" in buyer_html and "已取消" in buyer_html
    assert "共 1 条卖出记录" in seller_html
    assert trade["admin"].get("/reservations").status_code == 403
    stranger = app.test_client()
    assert login_as(stranger, "outsider").status_code == 303
    assert "共 0 条买入记录" in stranger.get(
        f"/reservations?side=buying&buyer_id={trade['ids']['other']}",
    ).get_data(as_text=True)
    assert app.test_client().get("/reservations").status_code == 303
    assert trade["buyer"].get("/reservations?status=EVIL").status_code == 400
    assert trade["buyer"].get("/reservations?side=everyone").status_code == 400


def test_failure_before_commit_leaves_no_success_marker(app, trade):
    """An interruption before commit rolls back even an already-flushed new reservation."""
    product_id = trade["product_id"]
    before = state_snapshot(app, product_id)
    key = new_key()
    token = token_for(trade["buyer"], "/account")

    def fail_commit(database):
        """Abort before a transaction containing a success marker can commit."""
        if any(isinstance(record, OperationRequestRecord) for record in database.new):
            raise RuntimeError("Injected pre-commit interruption")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="Injected pre-commit"):
            trade["buyer"].post(f"/products/{product_id}/reserve", data={
                "version": 2, "request_key": key, "csrf_token": token,
            })
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert state_snapshot(app, product_id) == before
    assert book(trade["buyer"], product_id, 2, key).status_code == 303


def test_cancelled_buyer_sees_snapshots_but_not_new_private_draft_content(app, trade):
    """History retains original text without exposing a seller's later private edits or image."""
    product_id = create_product(
        trade["seller"], intent="submit", image=(BytesIO(png_bytes()), "original.png"),
    )
    assert product_action(trade["admin"], product_id, 1, "approve").status_code == 303
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    assert act(trade["buyer"], record_id, "cancel", 1, reason="取消后保留历史").status_code == 303
    assert product_action(trade["seller"], product_id, 4, "return-draft").status_code == 303
    assert post_form(trade["seller"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "name": "新的私有草稿名称", "description": "新的私密联系方式",
        "version": 5, "image": (BytesIO(png_bytes()), "private-new.png"),
    }).status_code == 303
    history = trade["buyer"].get(f"/reservations/{record_id}")
    assert history.status_code == 200
    html = history.get_data(as_text=True)
    assert VALID_PRODUCT["name"] in html and VALID_PRODUCT["description"] in html
    assert "新的私有草稿名称" not in html and "新的私密联系方式" not in html
    assert trade["buyer"].get(f"/reservations/{record_id}/image").status_code == 404
    assert trade["seller"].get(f"/reservations/{record_id}/image").status_code == 200
    assert "新的私有草稿名称" not in trade["buyer"].get(
        "/reservations",
    ).get_data(as_text=True)
    assert_consistent(app, product_id)
