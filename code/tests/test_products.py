"""Seller draft, review, ownership, concurrency, and transaction integration tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import OperationLog, Product, ProductStatus
from tests.helpers import login_as, post_form, token_for
from tests.product_helpers import (
    VALID_PRODUCT, create_product, log_count, product_action, snapshot,
)


def test_incomplete_draft_stays_private(app, shop):
    """Incomplete drafts stay visible only to their seller and administrators."""
    product_id = create_product(shop["seller"], name="", category="", price="", description="")
    state = snapshot(app, product_id)
    assert state["status"] == ProductStatus.DRAFT
    assert state["price_cents"] is None
    assert state["name"] is None
    assert state["version"] == 1
    assert shop["seller"].get(f"/products/{product_id}").status_code == 200
    assert shop["admin"].get(f"/products/{product_id}").status_code == 200
    assert shop["other"].get(f"/products/{product_id}").status_code == 404
    assert app.test_client().get(f"/products/{product_id}").status_code == 404
    assert "未命名草稿" not in app.test_client().get("/products").get_data(as_text=True)


def test_complete_lifecycle_and_republication(app, shop):
    """Publication and re-publication require successful review; every state change is logged."""
    product_id = create_product(shop["seller"])
    assert product_action(shop["seller"], product_id, 1, "submit").status_code == 303
    assert product_action(shop["admin"], product_id, 2, "approve").status_code == 303
    first_time = snapshot(app, product_id)["published_at"]
    visitor = app.test_client()
    assert VALID_PRODUCT["name"] in visitor.get("/products").get_data(as_text=True)
    html = visitor.get(f"/products/{product_id}").get_data(as_text=True)
    assert VALID_PRODUCT["description"] in html
    assert product_action(shop["seller"], product_id, 3, "take-down").status_code == 303
    assert visitor.get(f"/products/{product_id}").status_code == 404
    assert VALID_PRODUCT["name"] not in visitor.get("/products").get_data(as_text=True)
    assert product_action(shop["seller"], product_id, 4, "submit").status_code == 409
    assert product_action(shop["seller"], product_id, 4, "return-draft").status_code == 303
    response = post_form(shop["seller"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "name": "重新审核的键盘", "version": "5", "intent": "submit",
    })
    assert response.status_code == 303
    assert snapshot(app, product_id)["status"] == ProductStatus.PENDING_REVIEW
    assert product_action(shop["admin"], product_id, 6, "approve").status_code == 303
    final = snapshot(app, product_id)
    assert final["version"] == 7
    assert final["published_at"] > first_time
    assert log_count(app) == 6


def test_save_and_submit_is_one_validated_transaction(app, shop):
    """A new complete submission creates a pending product and its log together."""
    product_id = create_product(shop["seller"], intent="submit", role="ADMIN", seller_id=1)
    state = snapshot(app, product_id)
    assert state["status"] == ProductStatus.PENDING_REVIEW
    assert state["seller_id"] == shop["ids"]["member"]
    assert state["version"] == 1
    assert log_count(app) == 1
    assert shop["seller"].get(f"/products/{product_id}/edit").status_code == 409


@pytest.mark.parametrize("field", ["name", "category", "price", "description"])
def test_incomplete_submission_preserves_input_without_saving(app, shop, field):
    """Failed publication points to the missing field and keeps the other text values."""
    response = post_form(shop["seller"], "/products/new", {
        **VALID_PRODUCT, field: "", "intent": "submit",
    })
    assert response.status_code == 400
    assert "提交审核前请填写" in response.get_data(as_text=True)
    if field != "description":
        assert VALID_PRODUCT["description"] in response.get_data(as_text=True)
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Product)) == 0
    assert log_count(app) == 0


@pytest.mark.parametrize("price", [
    "0", "-1", "1.234", "NaN", "Infinity", "1e2", "100000000", "abc",
])
def test_supplied_invalid_price_cannot_be_saved(app, shop, price):
    """Drafts may omit a price but cannot store malformed, rounded, or non-positive prices."""
    response = post_form(shop["seller"], "/products/new", {**VALID_PRODUCT, "price": price})
    assert response.status_code == 400
    assert VALID_PRODUCT["name"] in response.get_data(as_text=True)
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Product)) == 0


def test_pending_requires_withdrawal_and_rejection_requires_reason(app, shop):
    """Pending products cannot be edited; withdrawal and rejection both return a draft."""
    product_id = create_product(shop["seller"], intent="submit")
    original = snapshot(app, product_id)
    response = post_form(shop["seller"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "name": "未授权的直接修改", "version": 1,
    }, csrf_path=f"/products/{product_id}")
    assert response.status_code == 409
    assert snapshot(app, product_id) == original
    assert product_action(shop["admin"], product_id, 1, "reject", " ").status_code == 400
    assert snapshot(app, product_id) == original
    assert product_action(shop["seller"], product_id, 1, "withdraw").status_code == 303
    assert product_action(shop["admin"], product_id, 1, "approve").status_code == 409
    assert product_action(shop["seller"], product_id, 2, "submit").status_code == 303
    assert product_action(shop["admin"], product_id, 3, "reject", "请补充交付说明。").status_code == 303
    assert snapshot(app, product_id)["latest_reject_reason"] == "请补充交付说明。"
    assert "请补充交付说明。" in shop["seller"].get(f"/products/{product_id}").get_data(as_text=True)
    assert shop["seller"].get(f"/products/{product_id}/edit").status_code == 200


def test_foreign_owner_and_admin_cannot_use_seller_actions(app, shop):
    """Guessed IDs and forged seller/actor fields never grant ownership."""
    product_id = create_product(shop["seller"])
    original = snapshot(app, product_id)
    response = post_form(shop["other"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "version": 1, "seller_id": shop["ids"]["member"], "actor": 1,
    }, csrf_path="/account")
    # Private drafts deliberately appear nonexistent to another seller.
    assert response.status_code == 404
    for client in (shop["other"], shop["admin"]):
        response = post_form(client, f"/products/{product_id}/actions/submit", {
            "version": 1, "seller_id": shop["ids"]["member"],
        }, csrf_path="/account")
        assert response.status_code == 403
    assert snapshot(app, product_id) == original
    assert log_count(app) == 0
    assert shop["admin"].get("/products/new").status_code == 403


def test_member_cannot_approve_and_guest_cannot_create(app, shop):
    """Review permissions are enforced even when a request supplies a privileged role."""
    product_id = create_product(shop["seller"], intent="submit")
    response = post_form(shop["seller"], f"/products/{product_id}/actions/approve", {
        "version": 1, "role": "ADMIN", "actor": 1,
    }, csrf_path=f"/products/{product_id}")
    assert response.status_code == 403
    guest = app.test_client()
    response = post_form(guest, "/products/new", VALID_PRODUCT, csrf_path="/auth/login")
    assert response.status_code == 303
    assert snapshot(app, product_id)["status"] == ProductStatus.PENDING_REVIEW


def test_stale_edit_keeps_form_and_does_not_overwrite_new_data(app, shop):
    """An old form's version is never silently changed to force a successful retry."""
    product_id = create_product(shop["seller"])
    path = f"/products/{product_id}/edit"
    assert post_form(shop["seller"], path, {
        **VALID_PRODUCT, "name": "已成功保存的新名称", "version": "1",
    }).status_code == 303
    before = snapshot(app, product_id)
    response = post_form(shop["seller"], path, {
        **VALID_PRODUCT, "name": "旧标签页未保存内容", "version": "1",
    })
    assert response.status_code == 409
    html = response.get_data(as_text=True)
    assert "旧标签页未保存内容" in html
    assert 'name="version"' in html and 'value="1"' in html
    assert snapshot(app, product_id) == before


@pytest.mark.parametrize("state", [
    ProductStatus.RESERVED, ProductStatus.CONFIRMED, ProductStatus.COMPLETED, ProductStatus.BLOCKED,
])
def test_later_lifecycle_states_are_read_only_for_seller(app, shop, state):
    """Reserved, confirmed, completed, and blocked items cannot be edited or republished."""
    product_id = create_product(shop["seller"])
    with app.app_context():
        database = get_session()
        database.get(Product, product_id).status = state
        database.commit()
    before = snapshot(app, product_id)
    for operation in ("take-down", "return-draft", "submit"):
        assert product_action(shop["seller"], product_id, 1, operation).status_code == 409
    assert shop["seller"].get(f"/products/{product_id}/edit").status_code == 409
    assert snapshot(app, product_id) == before
    assert log_count(app) == 0


@pytest.mark.parametrize("other_operation", ["approve", "withdraw"])
def test_concurrent_review_has_only_one_winner(app, shop, other_operation):
    """Review versus withdrawal, and two reviewers, cannot both advance the same version."""
    product_id = create_product(shop["seller"], intent="submit")
    first = shop["admin"]
    if other_operation == "approve":
        second = app.test_client()
        assert login_as(second, "initial").status_code == 303
    else:
        second = shop["seller"]
    token_one = token_for(first, f"/products/{product_id}")
    token_two = token_for(second, f"/products/{product_id}")
    barrier = Barrier(2)

    def execute(arguments):
        """Issue independent requests against the same current product version."""
        client, token, operation = arguments
        barrier.wait(timeout=5)
        return client.post(f"/products/{product_id}/actions/{operation}", data={
            "csrf_token": token, "version": "1",
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(execute, [
            (first, token_one, "approve"), (second, token_two, other_operation),
        ]))
    assert sorted(statuses) == [303, 409]
    assert snapshot(app, product_id)["version"] == 2
    assert log_count(app) == 2


def test_failed_log_write_rolls_back_status_and_version(app, shop):
    """An injected log persistence failure cannot leave a product published without its log."""
    product_id = create_product(shop["seller"], intent="submit")
    original = snapshot(app, product_id)
    token = token_for(shop["admin"], f"/products/{product_id}")

    def fail_log(database, _context, _instances):
        """Simulate an interruption before the new log can be flushed."""
        if any(isinstance(record, OperationLog) for record in database.new):
            raise RuntimeError("Injected log write failure")

    event.listen(Session, "before_flush", fail_log)
    try:
        with pytest.raises(RuntimeError, match="Injected log"):
            shop["admin"].post(f"/products/{product_id}/actions/approve", data={
                "version": "1", "csrf_token": token,
            })
    finally:
        event.remove(Session, "before_flush", fail_log)
    assert snapshot(app, product_id) == original
    assert log_count(app) == 1


def test_csrf_and_invalid_versions_cannot_change_data(app, shop):
    """State changes require POST, a genuine CSRF token, and an explicit valid version."""
    product_id = create_product(shop["seller"])
    path = f"/products/{product_id}/actions/submit"
    original = snapshot(app, product_id)
    assert shop["seller"].get(path).status_code == 405
    assert shop["seller"].post(path, data={"version": 1}).status_code == 400
    for version in ("", "bad", "-1"):
        assert post_form(shop["seller"], path, {"version": version},
                         csrf_path=f"/products/{product_id}").status_code == 400
    assert snapshot(app, product_id) == original
    assert log_count(app) == 0
