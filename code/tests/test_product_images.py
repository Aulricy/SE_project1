"""File access, validation, replacement and rollback checks for optional images."""

from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app import create_app
from app.database import get_session
from app.models import Product
from tests.helpers import post_form, token_for
from tests.product_helpers import (
    VALID_PRODUCT, create_product, image_bytes, png_bytes, product_action, snapshot,
)


@pytest.mark.parametrize("format_name,extension,mime", [
    ("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp"),
])
def test_single_optional_image_follows_product_visibility(app, shop, format_name, extension, mime):
    """Actual image bytes determine the format, while product visibility controls access."""
    content = image_bytes(format_name)
    product_id = create_product(shop["seller"], image=(BytesIO(content), "../../unsafe.svg"))
    image_name = snapshot(app, product_id)["image_path"]
    assert image_name.endswith("." + extension) and "unsafe" not in image_name
    path = f"/products/{product_id}/image"
    visitor = app.test_client()
    assert visitor.get(path).status_code == 404
    assert shop["other"].get(path).status_code == 404
    assert shop["seller"].get(path).data == content
    assert shop["admin"].get(path).status_code == 200
    assert product_action(shop["seller"], product_id, 1, "submit").status_code == 303
    assert product_action(shop["admin"], product_id, 2, "approve").status_code == 303
    response = visitor.get(path)
    assert response.status_code == 200
    assert response.content_type == mime
    assert response.headers["Cache-Control"] == "no-store"
    assert product_action(shop["seller"], product_id, 3, "take-down").status_code == 303
    assert visitor.get(path).status_code == 404


@pytest.mark.parametrize("content,name", [
    (b"<svg><script>alert(1)</script></svg>", "picture.svg"),
    (b"not a real image", "picture.png"),
    (b"x" * (3 * 1024 * 1024 + 1), "large.png"),
])
def test_invalid_images_do_not_create_records_or_files(app, shop, content, name):
    """Reject image spoofing and oversized uploads before persistence."""
    response = post_form(shop["seller"], "/products/new", {
        **VALID_PRODUCT, "image": (BytesIO(content), name),
    })
    assert response.status_code == 400
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Product)) == 0
    assert not list(Path(app.config["UPLOAD_FOLDER"]).glob("*"))


def test_multiple_images_are_rejected(app, shop):
    """A repeated multipart file field must not bypass the one-image limit."""
    response = post_form(shop["seller"], "/products/new", {
        **VALID_PRODUCT, "image": [
            (BytesIO(png_bytes()), "one.png"), (BytesIO(png_bytes()), "two.png"),
        ],
    })
    assert response.status_code == 400
    assert "最多上传一张" in response.get_data(as_text=True)
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Product)) == 0


def test_image_replacement_failure_keeps_original_and_cleans_new_file(app, shop):
    """A failed database write cannot delete the old image or leave the new upload behind."""
    product_id = create_product(shop["seller"], image=(BytesIO(png_bytes()), "old.png"))
    before = snapshot(app, product_id)
    directory = Path(app.config["UPLOAD_FOLDER"])
    token = token_for(shop["seller"], f"/products/{product_id}/edit")

    def fail_update(database, _context, _instances):
        """Simulate failure after claiming the product but before committing its replacement."""
        if any(isinstance(record, Product) for record in database.dirty):
            raise RuntimeError("Injected image replacement failure")

    event.listen(Session, "before_flush", fail_update)
    try:
        with pytest.raises(RuntimeError, match="Injected image"):
            shop["seller"].post(f"/products/{product_id}/edit", data={
                **VALID_PRODUCT, "version": "1", "csrf_token": token,
                "image": (BytesIO(png_bytes()), "new.png"),
            })
    finally:
        event.remove(Session, "before_flush", fail_update)
    assert snapshot(app, product_id) == before
    assert [path.name for path in directory.iterdir()] == [before["image_path"]]


def test_replace_remove_and_restart_preserve_committed_images(app, shop):
    """Committed replacement survives app restart, then explicit removal unlinks the old file."""
    product_id = create_product(shop["seller"], image=(BytesIO(png_bytes()), "old.png"))
    first = snapshot(app, product_id)["image_path"]
    path = f"/products/{product_id}/edit"
    assert post_form(shop["seller"], path, {
        **VALID_PRODUCT, "version": 1, "image": (BytesIO(png_bytes()), "new.png"),
    }).status_code == 303
    second = snapshot(app, product_id)["image_path"]
    directory = Path(app.config["UPLOAD_FOLDER"])
    assert first != second
    assert not (directory / first).exists()
    assert (directory / second).exists()
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": app.config["SECRET_KEY"],
        "DATABASE_URL": app.config["DATABASE_URL"], "UPLOAD_FOLDER": str(directory),
    })
    try:
        client = restarted.test_client()
        cookie = shop["seller"].get_cookie(app.config["SESSION_COOKIE_NAME"])
        client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie.value)
        assert client.get(f"/products/{product_id}/image").status_code == 200
    finally:
        restarted.extensions["database_engine"].dispose()
    assert post_form(shop["seller"], path, {
        **VALID_PRODUCT, "version": 2, "remove_image": "y",
    }).status_code == 303
    assert snapshot(app, product_id)["image_path"] is None
    assert not (directory / second).exists()


def test_unauthorized_image_edit_does_not_leave_files(app, shop):
    """A foreign seller's failed write leaves both the product and upload directory unchanged."""
    product_id = create_product(shop["seller"])
    response = post_form(shop["other"], f"/products/{product_id}/edit", {
        **VALID_PRODUCT, "version": 1, "image": (BytesIO(png_bytes()), "attack.png"),
    }, csrf_path="/account")
    assert response.status_code == 404
    assert not list(Path(app.config["UPLOAD_FOLDER"]).glob("*"))
    assert snapshot(app, product_id)["image_path"] is None
