"""Product HTTP fixtures and snapshots; all data belongs to isolated test databases."""

from io import BytesIO

from PIL import Image
from sqlalchemy import func, select

from app.database import get_session
from app.models import OperationLog, Product
from tests.helpers import post_form

VALID_PRODUCT = {
    "name": "九成新键盘", "category": "DIGITAL", "price": "99.90",
    "description": "九成新，按描述自行联系，线下当面交付。",
}


def create_product(client, **changes) -> int:
    """Create a valid draft unless a test overrides the submission intent."""
    response = post_form(client, "/products/new", {**VALID_PRODUCT, **changes})
    assert response.status_code == 303, response.get_data(as_text=True)
    return int(response.location.rsplit("/", 1)[1])


def product_action(client, product_id: int, version: int, operation: str, reason: str = ""):
    """Exercise a real state-changing route with a genuine CSRF token."""
    return post_form(client, f"/products/{product_id}/actions/{operation}", {
        "version": str(version), "reason": reason,
    }, csrf_path=f"/products/{product_id}")


def snapshot(app, product_id: int) -> dict:
    """Return detached scalar values, allowing reliable before/after comparisons."""
    with app.app_context():
        product = get_session().get(Product, product_id)
        return {
            field: getattr(product, field) for field in (
                "name", "category", "description", "price_cents", "status", "version",
                "seller_id", "image_path", "published_at", "latest_reject_reason",
            )
        }


def log_count(app) -> int:
    """Count persisted transition records."""
    with app.app_context():
        return get_session().scalar(select(func.count()).select_from(OperationLog))


def image_bytes(format_name: str = "PNG") -> bytes:
    """Generate a small test image without any external image downloads."""
    output = BytesIO()
    Image.new("RGB", (20, 20), color=(45, 120, 80)).save(output, format=format_name)
    return output.getvalue()


def png_bytes() -> bytes:
    """Return the default PNG fixture for replacement and validation tests."""
    return image_bytes()
