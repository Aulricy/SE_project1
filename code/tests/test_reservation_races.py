"""Independent-client concurrency checks against a real on-disk SQLite database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.services.users import UserService
from tests.helpers import PASSWORD, login_as, token_for
from tests.product_helpers import create_product, product_action
from tests.reservation_helpers import (
    act, assert_consistent, book, command_context, new_key, reservation_id, state_snapshot,
)


def _race(commands):
    """Release all independent client requests at the same instant."""
    barrier = Barrier(len(commands))

    def send(command):
        """Each worker has its own Flask client and database session."""
        client, path, payload = command
        barrier.wait(timeout=10)
        response = client.post(path, data=payload)
        return response.status_code, response.location

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        return list(executor.map(send, commands))


def _clones(app, client, count):
    """Clone an authenticated browser cookie, without sharing client state between threads."""
    token = token_for(client, "/account")
    cookie_name = app.config["SESSION_COOKIE_NAME"]
    cookie = client.get_cookie(cookie_name)
    clones = [app.test_client() for _number in range(count)]
    for clone in clones:
        clone.set_cookie(cookie_name, cookie.value)
    return clones, token


def test_ten_buyers_have_exactly_one_booking_winner(app, trade):
    """Ten different actors cannot create more than one valid reservation for one product."""
    product_id = trade["product_id"]
    clients = [trade["buyer"]]
    for number in range(1, 10):
        username = f"racing_buyer_{number}"
        with app.app_context():
            UserService.register(username, PASSWORD)
        client = app.test_client()
        assert login_as(client, username).status_code == 303
        clients.append(client)
    commands = [
        (client, f"/products/{product_id}/reserve", {
            "version": 2, "request_key": new_key(), "csrf_token": token_for(client, "/account"),
        }) for client in clients
    ]
    outcomes = _race(commands)
    assert sorted(status for status, _location in outcomes) == [303] + [409] * 9
    final = assert_consistent(app, product_id)
    assert len(final["reservations"]) == 1
    assert final["requests"] == 1 and final["logs"] == 3
    assert final["product"][:2] == ("RESERVED", 3)


def test_ten_concurrent_replays_return_one_original_booking(app, trade):
    """The same actor and exact request return one result even under simultaneous arrival."""
    product_id = trade["product_id"]
    clients, token = _clones(app, trade["buyer"], 10)
    key = new_key()
    outcomes = _race([
        (client, f"/products/{product_id}/reserve", {
            "version": 2, "request_key": key, "csrf_token": token,
        }) for client in clients
    ])
    assert [status for status, _location in outcomes] == [303] * 10
    assert len({location for _status, location in outcomes}) == 1
    final = assert_consistent(app, product_id)
    assert len(final["reservations"]) == 1
    assert final["requests"] == 1 and final["logs"] == 3
    assert final["product"][:2] == ("RESERVED", 3)


def test_same_actor_key_cannot_concurrently_book_different_products(app, trade):
    """Actor/key uniqueness applies across products, not merely within a product row lock."""
    first_id = trade["product_id"]
    second_id = create_product(trade["seller"], intent="submit")
    assert product_action(trade["admin"], second_id, 1, "approve").status_code == 303
    clients, token = _clones(app, trade["buyer"], 2)
    key = new_key()
    outcomes = _race([
        (client, f"/products/{product_id}/reserve", {
            "version": 2, "request_key": key, "csrf_token": token,
        }) for client, product_id in zip(clients, (first_id, second_id))
    ])
    assert sorted(status for status, _location in outcomes) == [303, 409]
    snapshots = [assert_consistent(app, product_id) for product_id in (first_id, second_id)]
    assert sorted(item["product"][0] for item in snapshots) == ["PUBLISHED", "RESERVED"]
    assert sum(len(item["reservations"]) for item in snapshots) == 1
    assert snapshots[0]["requests"] == 1


def test_confirmation_versus_old_cancellation_has_one_winner(app, trade):
    """A stale pending cancel cannot also succeed after confirmation advances the version."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    commands = []
    for actor, operation in (("seller", "confirm"), ("buyer", "cancel")):
        client = trade[actor]
        commands.append((client, f"/reservations/{record_id}/actions/{operation}", {
            "version": 1, "request_key": new_key(), "reason": "并发取消原因",
            "csrf_token": token_for(client, "/account"),
        }))
    outcomes = _race(commands)
    assert sorted(status for status, _location in outcomes) == [303, 409]
    final = assert_consistent(app, product_id)
    assert final["reservations"][0][3] == 2
    assert final["requests"] == 2 and final["logs"] == 4
    if final["reservations"][0][2] == "CONFIRMED":
        assert act(trade["buyer"], record_id, "cancel", 2, reason="刷新后取消").status_code == 303
    else:
        assert final["reservations"][0][2] == "CANCELLED"
        assert act(trade["seller"], record_id, "confirm", 2).status_code == 409
    assert_consistent(app, product_id)


def test_completion_versus_cancellation_has_one_winner(app, trade):
    """Completion and cancellation cannot both succeed from the same confirmed page."""
    product_id = trade["product_id"]
    record_id = reservation_id(book(trade["buyer"], product_id, 2))
    assert act(trade["seller"], record_id, "confirm", 1).status_code == 303
    commands = []
    for actor, operation in (("buyer", "complete"), ("seller", "cancel")):
        client = trade[actor]
        commands.append((client, f"/reservations/{record_id}/actions/{operation}", {
            "version": 2, "request_key": new_key(), "reason": "并发取消原因",
            "csrf_token": token_for(client, "/account"),
        }))
    outcomes = _race(commands)
    assert sorted(status for status, _location in outcomes) == [303, 409]
    final = assert_consistent(app, product_id)
    assert final["reservations"][0][3] == 3
    assert final["requests"] == 3 and final["logs"] == 5
    row = final["reservations"][0]
    if row[2] == "COMPLETED":
        assert row[8] is not None and row[7] is None
    else:
        assert row[2] == "CANCELLED" and row[7] is not None and row[8] is None


def test_booking_versus_takedown_has_one_winner(app, trade):
    """A product cannot be successfully booked and taken down from one page version."""
    product_id = trade["product_id"]
    outcomes = _race([
        (trade["buyer"], f"/products/{product_id}/reserve", {
            "version": 2, "request_key": new_key(),
            "csrf_token": token_for(trade["buyer"], "/account"),
        }),
        (trade["seller"], f"/products/{product_id}/actions/take-down", {
            "version": 2, "csrf_token": token_for(trade["seller"], "/account"),
        }),
    ])
    assert sorted(status for status, _location in outcomes) == [303, 409]
    final = assert_consistent(app, product_id)
    assert final["product"][0] in {"RESERVED", "OFF_SHELF"}
    assert final["product"][1] == 3
    assert len(final["reservations"]) == (1 if final["product"][0] == "RESERVED" else 0)
    assert final["logs"] == 3


def test_old_reserve_and_cancel_replays_cannot_touch_a_new_active_record(app, trade):
    """Simultaneous old-success replays are read-only with respect to a newer reservation."""
    product_id = trade["product_id"]
    old_key, cancel_key = new_key(), new_key()
    old_id = reservation_id(book(trade["buyer"], product_id, 2, old_key))
    assert act(trade["buyer"], old_id, "cancel", 1, cancel_key, reason="取消旧预约").status_code == 303
    new_id = reservation_id(book(trade["buyer"], product_id, 4))
    before = state_snapshot(app, product_id)
    clients, token = _clones(app, trade["buyer"], 2)
    outcomes = _race([
        (clients[0], f"/products/{product_id}/reserve", {
            "version": 2, "request_key": old_key, "csrf_token": token,
        }),
        (clients[1], f"/reservations/{old_id}/actions/cancel", {
            "version": 1, "request_key": cancel_key, "reason": "取消旧预约", "csrf_token": token,
        }),
    ])
    assert [status for status, _location in outcomes] == [303, 303]
    assert all(location.endswith(f"/{old_id}") for _status, location in outcomes)
    assert old_id != new_id
    assert state_snapshot(app, product_id) == before
    assert_consistent(app, product_id)


@pytest.mark.parametrize("operation", ["confirm", "cancel", "complete"])
def test_concurrent_duplicate_trade_commands_execute_once(app, trade, operation):
    """Identical confirmation, cancellation and completion commands each log only once."""
    product_id, record_id, version, actor = command_context(trade, operation)
    before = state_snapshot(app, product_id)
    clients, token = _clones(app, actor, 10)
    key = new_key()
    outcomes = _race([
        (client, f"/reservations/{record_id}/actions/{operation}", {
            "version": version, "request_key": key, "reason": "相同取消请求",
            "csrf_token": token,
        }) for client in clients
    ])
    assert [status for status, _location in outcomes] == [303] * 10
    assert len({location for _status, location in outcomes}) == 1
    final = assert_consistent(app, product_id)
    assert final["product"][1] == before["product"][1] + 1
    assert final["reservations"][0][3] == before["reservations"][0][3] + 1
    assert final["requests"] == before["requests"] + 1
    assert final["logs"] == before["logs"] + 1
