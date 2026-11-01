"""Real HTTP moderation races against isolated SQLite, including duplicate admin submissions."""

from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

from sqlalchemy.orm import Session
from werkzeug.serving import make_server

from app.models import Product, ProductStatus, Reservation
from app.services.users import UserService
from tools.verify_reservation_races import Client, QuietHandler, _prepare_fixture, race, snapshot


def _record(outcomes, engine, pid, expected_logs, expected_version):
    """Keep all HTTP outcomes and verify committed paired states, versions and audit count."""
    state = snapshot(engine, pid)
    records = state["reservations"]
    active = [row for row in records if row["status"] in {"PENDING_CONFIRMATION", "CONFIRMED"}]
    expected = {"RESERVED": "PENDING_CONFIRMATION", "CONFIRMED": "CONFIRMED"}.get(
        state["product_status"],
    )
    consistent = (len(active) == 1 and active[0]["status"] == expected) if expected else not active
    with Session(engine) as database:
        cancellations_complete = all(
            record.cancelled_at and record.cancelled_by and record.cancel_reason
            for record in database.query(Reservation).filter_by(product_id=pid, status="CANCELLED")
        )
    passed = (
        sorted(row["status"] for row in outcomes) == [303] + [409] * (len(outcomes) - 1)
        and consistent and cancellations_complete
        and state["operation_logs"] == expected_logs
        and state["product_version"] == expected_version
    )
    return {"passed": bool(passed), "http": outcomes, "state": state}


def _trade_case(engine, clients, pid, operation):
    """Run one block-versus-trade race from explicit initial versions."""
    admin, seller, buyer = clients
    version, rid, record_version = 1, None, 1
    if operation != "reserve":
        response = buyer.post(f"/products/{pid}/reserve", {
            "version": 1, "request_key": uuid4().hex,
        })
        assert response["status"] == 303
        rid, version = int(response["location"].rsplit("/", 1)[1]), 2
    if operation == "complete":
        assert seller.post(f"/reservations/{rid}/actions/confirm", {
            "version": 1, "request_key": uuid4().hex,
        })["status"] == 303
        version, record_version = 3, 2
    actor = seller if operation == "confirm" else buyer
    path = (f"/products/{pid}/reserve" if operation == "reserve"
            else f"/reservations/{rid}/actions/{operation}")
    outcomes = race([
        (admin.clone(), f"/products/{pid}/actions/block", {
            "version": version, "reason": "真实 HTTP 并发屏蔽原因",
        }),
        (actor.clone(), path, {
            "version": version if operation == "reserve" else record_version,
            "request_key": uuid4().hex, "reason": "真实 HTTP 并发取消原因",
        }),
    ])
    result = _record(outcomes, engine, pid, version, version + 1)
    if rid:
        result["passed"] &= result["state"]["reservations"][0]["version"] == record_version + 1
    return {"case": "block_vs_" + operation, **result}


def _run(engine, admin, seller, buyer, products):
    """Collect four competing trade cases and two ten-client duplicate admin cases."""
    cases = []
    for index, operation in enumerate(("reserve", "confirm", "cancel", "complete")):
        cases.append(_trade_case(engine, (admin, seller, buyer), products[index], operation))
    pid = products[4]
    response = buyer.post(f"/products/{pid}/reserve", {"version": 1, "request_key": uuid4().hex})
    assert response["status"] == 303
    rid = int(response["location"].rsplit("/", 1)[1])
    assert seller.post(f"/reservations/{rid}/actions/confirm", {
        "version": 1, "request_key": uuid4().hex,
    })["status"] == 303
    for operation, version in (("block", 3), ("restore", 4)):
        outcomes = race([(admin.clone(), f"/products/{pid}/actions/{operation}", {
            "version": version, "reason": "十个重复管理员请求",
        }) for _number in range(10)])
        result = _record(outcomes, engine, pid, version, version + 1)
        expected = "BLOCKED" if operation == "block" else "DRAFT"
        result["passed"] &= result["state"]["product_status"] == expected
        result["passed"] &= result["state"]["reservations"][0]["status"] == "CANCELLED"
        result["passed"] &= result["state"]["reservations"][0]["version"] == 3
        cases.append({"case": "ten_duplicate_" + operation, **result})
    return cases


def _http_cases(application, engine, products):
    """Serve the fixture while independent authenticated clients exercise its routes."""
    with ExitStack() as cleanup:
        cleanup.callback(engine.dispose)
        server = make_server(
            "127.0.0.1", 0, application, threaded=True, request_handler=QuietHandler,
        )
        cleanup.callback(server.server_close)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        cleanup.callback(worker.join, timeout=5)
        cleanup.callback(server.shutdown)
        clients = [Client(f"http://127.0.0.1:{server.server_port}") for _number in range(3)]
        for client, username in zip(clients, ("http_admin", "http_seller", "http_buyer_0")):
            client.login(username)
        return _run(engine, *clients, products)


def verify():
    """Serve generated users/products locally; remove the temporary database after all checks."""
    parent = Path(__file__).resolve().parents[1] / "instance"
    with TemporaryDirectory(prefix="admin-http-", dir=parent) as temporary:
        workspace = Path(temporary).resolve()
        assert workspace.is_relative_to(parent.resolve())
        application, engine, products = _prepare_fixture(workspace)
        with application.app_context():
            UserService.bootstrap_admin("http_admin", "HttpTestPassword123!")
            with Session(engine) as database, database.begin():
                source = database.get(Product, products[0])
                extra = [Product(
                    seller_id=source.seller_id, name="额外 HTTP 核验商品", price_cents=3050,
                    description="生成的隔离数据", status=ProductStatus.PUBLISHED,
                ) for _number in range(2)]
                database.add_all(extra)
                database.flush()
                products.extend(row.id for row in extra)
        cases = _http_cases(application, engine, products)
    return {
        "passed": all(case["passed"] for case in cases), "cases": cases,
        "racing_http_requests": sum(len(case["http"]) for case in cases),
        "scope": "Six correctness cases, real threaded HTTP, independent clients, file SQLite.",
        "data": "Generated fixtures in a temporary database; everyday database untouched.",
    }


def main():
    """Persist the complete outcome set and fail if any invariant or HTTP outcome is wrong."""
    report = verify()
    destination = Path(__file__).resolve().parents[1] / "instance" / "admin-race-check.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
