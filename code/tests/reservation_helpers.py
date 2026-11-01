"""HTTP commands and persisted snapshots for isolated reservation checks."""

from uuid import uuid4

from sqlalchemy import func, select

from app.database import get_session
from app.models import OperationLog, OperationRequestRecord, Product, Reservation
from tests.helpers import post_form


def new_key() -> str:
    """Return a fresh user-operation identity."""
    return uuid4().hex


def book(client, product_id: int, version: int, key: str | None = None, **extra):
    """POST an explicit product page version with real CSRF protection."""
    return post_form(client, f"/products/{product_id}/reserve", {
        "version": version, "request_key": key or new_key(), **extra,
    }, csrf_path="/account")


def act(client, record_id: int, operation: str, version: int,
        key: str | None = None, **extra):
    """POST an explicit reservation page version, retaining its request identity."""
    return post_form(client, f"/reservations/{record_id}/actions/{operation}", {
        "version": version, "request_key": key or new_key(),
        "reason": extra.pop("reason", ""), **extra,
    }, csrf_path="/account")


def reservation_id(response) -> int:
    """Require a successful redirect to the resulting reservation."""
    assert response.status_code == 303, response.get_data(as_text=True)
    return int(response.location.rsplit("/", 1)[1])


def state_snapshot(app, product_id: int) -> dict:
    """Compare all business/request/log state before and after failures or replays."""
    with app.app_context():
        database = get_session()
        product = database.get(Product, product_id)
        reservations = list(database.scalars(
            select(Reservation).where(Reservation.product_id == product_id)
            .order_by(Reservation.id),
        ))
        return {
            "product": (product.status, product.version, product.price_cents),
            "reservations": [
                (record.id, record.buyer_id, record.status, record.version,
                 record.reserved_price_cents, record.created_at, record.confirmed_at,
                 record.cancelled_at, record.completed_at,
                 record.cancelled_by, record.cancel_reason,
                 record.reserved_name, record.reserved_description)
                for record in reservations
            ],
            "requests": database.scalar(select(func.count()).select_from(OperationRequestRecord)),
            "logs": database.scalar(select(func.count()).select_from(OperationLog)),
        }


def assert_consistent(app, product_id: int):
    """An occupied product has exactly one matching active reservation."""
    snapshot = state_snapshot(app, product_id)
    active = [row for row in snapshot["reservations"] if row[2] in {
        "PENDING_CONFIRMATION", "CONFIRMED",
    }]
    assert len(active) <= 1
    if snapshot["product"][0] == "RESERVED":
        assert len(active) == 1 and active[0][2] == "PENDING_CONFIRMATION"
    elif snapshot["product"][0] == "CONFIRMED":
        assert len(active) == 1 and active[0][2] == "CONFIRMED"
    else:
        assert not active
    return snapshot


def command_context(trade, operation: str):
    """Prepare a real pending/confirmed HTTP trade for failure and duplicate-command tests."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    version = 1
    if operation == "complete":
        assert act(trade["seller"], record_id, "confirm", 1).status_code == 303
        version = 2
    actor = trade["seller"] if operation == "confirm" else trade["buyer"]
    return product_id, record_id, version, actor
