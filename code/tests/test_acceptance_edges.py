"""Cross-module acceptance regressions found by checking the experiment-two design."""

from io import BytesIO
from pathlib import Path
import re
import sqlite3

import pytest

from app.database import get_engine
from tests.helpers import post_form, token_for
from tests.product_helpers import VALID_PRODUCT, create_product, png_bytes, product_action, snapshot
from tests.reservation_helpers import book, reservation_id, state_snapshot

OVERSIZED_INTEGER = "9223372036854775808"


@pytest.mark.parametrize("path", [
    "/products/{identifier}", "/products/{identifier}/image", "/products/{identifier}/edit",
    "/reservations/{identifier}", "/reservations/{identifier}/image",
])
def test_out_of_range_detail_ids_are_unavailable_instead_of_crashing(shop, path):
    """Unknown route identifiers cannot overflow SQLite parameter binding."""
    assert shop["seller"].get(path.format(identifier=OVERSIZED_INTEGER)).status_code == 404


@pytest.mark.parametrize("path", [
    "/products/{identifier}/reserve", "/products/{identifier}/actions/take-down",
    "/reservations/{identifier}/actions/confirm",
])
def test_out_of_range_command_ids_do_not_reach_database_binding(app, trade, path):
    """Malformed targets cannot write partial product, reservation, request or audit rows."""
    before = state_snapshot(app, trade["product_id"])
    response = post_form(trade["buyer"], path.format(identifier=OVERSIZED_INTEGER), {
        "version": 1, "request_key": "oversized_target_test_request",
    }, csrf_path="/account")
    assert response.status_code == 404
    assert state_snapshot(app, trade["product_id"]) == before


@pytest.mark.parametrize("operation", ["edit", "submit", "reserve", "confirm", "block"])
def test_out_of_range_versions_are_explained_without_saving(app, trade, operation):
    """Every module bounds versions consistently before a SQL comparison."""
    pid = trade["product_id"]
    actor = trade["seller"]
    path = f"/products/{pid}/actions/{operation}"
    data = {"version": OVERSIZED_INTEGER, "request_key": "oversized_version_test_request",
            "reason": "不得保存"}
    if operation == "edit":
        path, data = f"/products/{pid}/edit", {**VALID_PRODUCT, **data}
    elif operation == "reserve":
        actor, path = trade["buyer"], f"/products/{pid}/reserve"
    elif operation == "confirm":
        rid = reservation_id(book(trade["buyer"], pid, 2))
        path = f"/reservations/{rid}/actions/confirm"
    elif operation == "block":
        actor = trade["admin"]
    before = state_snapshot(app, pid)
    response = post_form(actor, path, data, csrf_path="/account")
    assert response.status_code == 400
    assert "版本无效" in response.get_data(as_text=True)
    assert state_snapshot(app, pid) == before


def test_reservation_filter_error_preserves_selling_scope_and_rejects_overflow(app, trade):
    """Bad object filters retain the selected seller scope, without a database overflow."""
    before = state_snapshot(app, trade["product_id"])
    response = trade["seller"].get("/reservations", query_string={
        "side": "selling", "product_id": OVERSIZED_INTEGER,
    })
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'name="side" value="selling"' in html
    assert "条卖出记录" in html and f'value="{OVERSIZED_INTEGER}"' in html
    assert state_snapshot(app, trade["product_id"]) == before


def test_failed_rejection_retains_reason_and_original_version(app, shop):
    """An old pending review cannot silently become a rejection of a later submission."""
    pid = create_product(shop["seller"], intent="submit")
    assert product_action(shop["seller"], pid, 1, "withdraw").status_code == 303
    assert product_action(shop["seller"], pid, 2, "submit").status_code == 303
    before = state_snapshot(app, pid)
    reason = "旧审核意见，需要保留以便核对"
    response = product_action(shop["admin"], pid, 1, "reject", reason)
    assert response.status_code == 409
    form = re.search(r'<form[^>]+action="[^"]+/reject"[^>]*>(.*?)</form>',
                     response.get_data(as_text=True), re.S).group(1)
    assert f'>{reason}</textarea>' in form
    assert 'name="version" value="1"' in form
    assert state_snapshot(app, pid) == before


@pytest.mark.parametrize("operation", ["create", "edit", "submit"])
def test_locked_product_writes_return_conflict_and_preserve_all_state(app, shop, operation):
    """An actual independent SQLite writer holds the lock while an HTTP write is attempted."""
    pid = create_product(shop["seller"])
    before = state_snapshot(app, pid)
    token = token_for(shop["seller"], "/account")
    path = f"/products/{pid}/actions/submit"
    data = {"version": 1, "csrf_token": token}
    if operation in {"create", "edit"}:
        path = "/products/new" if operation == "create" else f"/products/{pid}/edit"
        data.update(VALID_PRODUCT)
        data["image"] = (BytesIO(png_bytes()), "failed-write.png")
    with app.app_context():
        engine = get_engine()
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=50")
        filename = engine.url.database
    with sqlite3.connect(filename, autocommit=True) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = shop["seller"].post(path, data=data)
        finally:
            blocker.execute("ROLLBACK")
    assert response.status_code == 409
    assert "其他操作" in response.get_data(as_text=True)
    assert state_snapshot(app, pid) == before
    assert not list(Path(app.config["UPLOAD_FOLDER"]).glob("*"))


def test_upload_directory_failure_is_actionable_and_keeps_text(app, shop, monkeypatch):
    """Failure to create an image directory is reported before any product is saved."""
    directory = Path(app.config["UPLOAD_FOLDER"]).resolve()
    original = Path.mkdir

    def deny_uploads(path, *args, **kwargs):
        if path.resolve() == directory:
            raise OSError("Injected upload directory failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_uploads)
    response = post_form(shop["seller"], "/products/new", {
        **VALID_PRODUCT, "image": (BytesIO(png_bytes()), "directory-error.png"),
    }, csrf_path="/account")
    assert response.status_code == 503
    assert "图片保存失败" in response.get_data(as_text=True)
    assert VALID_PRODUCT["name"] in response.get_data(as_text=True)
    assert not directory.exists()


def test_oversized_form_has_a_clear_response_without_mutation(app, trade):
    """Request-size rejection does not become an untranslated error or a partial edit."""
    before = snapshot(app, trade["product_id"])
    token = token_for(trade["seller"], "/account")
    app.config["MAX_CONTENT_LENGTH"] = 1024
    response = trade["seller"].post("/products/new", data={
        **VALID_PRODUCT, "description": "x" * 2048, "csrf_token": token,
    })
    assert response.status_code == 413
    assert "提交内容过大" in response.get_data(as_text=True)
    assert snapshot(app, trade["product_id"]) == before
