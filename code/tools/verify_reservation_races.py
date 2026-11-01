"""Real HTTP verification of SQLite booking races, durable replay and paired-state consistency."""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from werkzeug.serving import WSGIRequestHandler, make_server

from app import create_app
from app.database import get_engine
from app.models import (
    OperationLog, OperationRequestRecord, Product, ProductCategory, ProductStatus, Reservation,
)
from app.models.base import utc_now
from app.services.users import UserService


class QuietHandler(WSGIRequestHandler):
    """Keep test traffic separate from the normal app's access log."""

    def log_request(self, code="-", size="-"):
        """Record outcomes in the JSON check instead."""


class NoRedirect(HTTPRedirectHandler):
    """Retain the actual 303 or 409 instead of reporting a following page's 200."""

    def redirect_request(self, *_args, **_kwargs):
        """Return the original redirect response to the verification client."""
        return None


class Client:
    """An independent HTTP cookie jar and a genuine server-issued CSRF token."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), NoRedirect())
        self.csrf = ""

    def request(self, path: str, data=None):
        """Read redirects and expected conflict errors without excluding them from results."""
        encoded = urlencode(data).encode() if data is not None else None
        request = Request(self.base_url + path, data=encoded)
        return self.send_request(request)

    def send_request(self, request):
        """Share transport and error handling between URL-encoded and multipart form checks."""
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.read().decode("utf-8"), response.headers.get("Location")

    def login(self, username: str):
        """Authenticate through the actual public login form."""
        status, html, _location = self.request("/auth/login")
        assert status == 200
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)
        status, _html, _location = self.request("/auth/login", {
            "csrf_token": token, "username": username, "password": "HttpTestPassword123!",
        })
        assert status == 303
        status, html, _location = self.request("/account")
        assert status == 200
        self.csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)

    def clone(self):
        """Copy browser authentication while keeping concurrent client state independent."""
        client = Client(self.base_url)
        for cookie in self.cookies:
            client.cookies.set_cookie(copy(cookie))
        client.csrf = self.csrf
        return client

    def post(self, path: str, data: dict) -> dict:
        """Retain transport errors as failed outcomes instead of dropping requests."""
        start = perf_counter()
        try:
            status, _html, location = self.request(path, {"csrf_token": self.csrf, **data})
            error = None
        except (URLError, OSError, TimeoutError) as exception:
            status, location, error = 0, None, type(exception).__name__
        return {
            "status": status, "location": location,
            "seconds": round(perf_counter() - start, 6), "error": error,
        }


def race(commands: list) -> list[dict]:
    """Start every HTTP request from an independent client at the same barrier."""
    barrier = Barrier(len(commands))

    def send(command):
        """Issue one real network request after all clients are ready."""
        client, path, data = command
        barrier.wait(timeout=10)
        return client.post(path, data)

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        return list(executor.map(send, commands))


def snapshot(engine, product_id: int) -> dict:
    """Check state pairs and counts from committed data, not from HTTP text."""
    with Session(engine) as database:
        product = database.get(Product, product_id)
        records = list(database.scalars(
            select(Reservation).where(Reservation.product_id == product_id)
            .order_by(Reservation.id),
        ))
        ids = [record.id for record in records]
        requests = database.scalar(
            select(func.count()).select_from(OperationRequestRecord)
            .where(OperationRequestRecord.reservation_id.in_(ids)),
        )
        logs = database.scalar(
            select(func.count()).select_from(OperationLog)
            .where(OperationLog.product_id == product_id),
        )
        return {
            "product_status": product.status.value, "product_version": product.version,
            "reservations": [
                {"id": record.id, "status": record.status.value, "version": record.version,
                 "reserved_price_cents": record.reserved_price_cents,
                 "completed_at": record.completed_at.isoformat() if record.completed_at else None}
                for record in records
            ],
            "success_requests": requests, "operation_logs": logs,
        }


def _run_cases(engine, seller: Client, buyers: list[Client], products: list[int]) -> list[dict]:
    """Use separate products so each race has a clear starting state and complete outcome set."""
    cases = []
    product_id = products[0]
    outcomes = race([
        (buyer, f"/products/{product_id}/reserve", {"version": 1, "request_key": uuid4().hex})
        for buyer in buyers
    ])
    state = snapshot(engine, product_id)
    passed = (
        sorted(row["status"] for row in outcomes) == [303] + [409] * 9
        and len(state["reservations"]) == state["success_requests"] == state["operation_logs"] == 1
        and (state["product_status"], state["product_version"]) == ("RESERVED", 2)
    )
    cases.append({
        "case": "ten_different_buyers", "passed": passed, "http": outcomes, "state": state,
    })
    winner = buyers[next(index for index, result in enumerate(outcomes) if result["status"] == 303)]
    record_id = state["reservations"][0]["id"]
    outcomes = race([
        (seller, f"/reservations/{record_id}/actions/confirm",
         {"version": 1, "request_key": uuid4().hex}),
        (winner, f"/reservations/{record_id}/actions/cancel",
         {"version": 1, "request_key": uuid4().hex, "reason": "并发取消测试"}),
    ])
    state = snapshot(engine, product_id)
    pair = (state["product_status"], state["reservations"][0]["status"])
    passed = (
        sorted(row["status"] for row in outcomes) == [303, 409]
        and pair in {("CONFIRMED", "CONFIRMED"), ("PUBLISHED", "CANCELLED")}
        and state["reservations"][0]["version"] == 2
        and state["success_requests"] == state["operation_logs"] == 2
    )
    cases.append({"case": "confirmation_vs_cancellation", "passed": passed,
                  "http": outcomes, "state": state})

    product_id = products[1]
    key = uuid4().hex
    outcomes = race([
        (buyers[0].clone(), f"/products/{product_id}/reserve",
         {"version": 1, "request_key": key}) for _number in range(10)
    ])
    state = snapshot(engine, product_id)
    passed = (
        [row["status"] for row in outcomes] == [303] * 10
        and len({row["location"] for row in outcomes}) == 1
        and len(state["reservations"]) == state["success_requests"] == state["operation_logs"] == 1
        and state["product_version"] == 2
    )
    cases.append({"case": "ten_identical_booking_replays", "passed": passed,
                  "http": outcomes, "state": state})
    record_id = state["reservations"][0]["id"]
    assert seller.post(f"/reservations/{record_id}/actions/confirm", {
        "version": 1, "request_key": uuid4().hex,
    })["status"] == 303
    key = uuid4().hex
    outcomes = race([
        (buyers[0].clone(), f"/reservations/{record_id}/actions/complete",
         {"version": 2, "request_key": key}) for _number in range(10)
    ])
    state = snapshot(engine, product_id)
    passed = (
        [row["status"] for row in outcomes] == [303] * 10
        and len({row["location"] for row in outcomes}) == 1
        and state["product_status"] == state["reservations"][0]["status"] == "COMPLETED"
        and state["reservations"][0]["completed_at"] is not None
        and state["product_version"] == 4
        and state["success_requests"] == state["operation_logs"] == 3
    )
    cases.append({"case": "ten_identical_completion_replays", "passed": passed,
                  "http": outcomes, "state": state})

    product_id = products[2]
    result = buyers[0].post(f"/products/{product_id}/reserve", {
        "version": 1, "request_key": uuid4().hex,
    })
    assert result["status"] == 303
    record_id = int(result["location"].rsplit("/", 1)[1])
    assert seller.post(f"/reservations/{record_id}/actions/confirm", {
        "version": 1, "request_key": uuid4().hex,
    })["status"] == 303
    outcomes = race([
        (buyers[0], f"/reservations/{record_id}/actions/complete",
         {"version": 2, "request_key": uuid4().hex}),
        (seller, f"/reservations/{record_id}/actions/cancel",
         {"version": 2, "request_key": uuid4().hex, "reason": "完成与取消竞争测试"}),
    ])
    state = snapshot(engine, product_id)
    pair = (state["product_status"], state["reservations"][0]["status"])
    passed = (
        sorted(row["status"] for row in outcomes) == [303, 409]
        and pair in {("COMPLETED", "COMPLETED"), ("PUBLISHED", "CANCELLED")}
        and state["success_requests"] == state["operation_logs"] == 3
    )
    cases.append({"case": "completion_vs_cancellation", "passed": passed,
                  "http": outcomes, "state": state})
    return cases


def _prepare_fixture(workspace: Path):
    """Generate isolated users and products separately from HTTP orchestration."""
    application = create_app({
        "SECRET_KEY": uuid4().hex, "SESSION_COOKIE_NAME": "reservation_verify_session",
        "DATABASE_URL": "sqlite:///" + (workspace / "check.sqlite3").as_posix(),
        "UPLOAD_FOLDER": str(workspace / "uploads"),
    })
    result = application.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.exception
    with application.app_context():
        seller_record = UserService.register("http_seller", "HttpTestPassword123!")
        for number in range(10):
            UserService.register(f"http_buyer_{number}", "HttpTestPassword123!")
        engine = get_engine()
        with Session(engine, expire_on_commit=False) as database, database.begin():
            products = [
                Product(
                    seller_id=seller_record.id, name=f"并发核验商品 {number}",
                    category=ProductCategory.DIGITAL, price_cents=9990,
                    description="隔离生成的 HTTP 并发测试数据，不是实际商品。",
                    status=ProductStatus.PUBLISHED, published_at=utc_now(),
                ) for number in range(3)
            ]
            database.add_all(products)
            database.flush()
            product_ids = [product.id for product in products]
    return application, engine, product_ids


def verify() -> dict:
    """Run real HTTP checks on generated data without touching the everyday database."""
    parent = Path(__file__).resolve().parents[1] / "instance"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="reservation-http-", dir=parent) as temporary:
        workspace = Path(temporary).resolve()
        assert workspace.is_relative_to(parent.resolve())
        application, engine, product_ids = _prepare_fixture(workspace)
        server = make_server(
            "127.0.0.1", 0, application, threaded=True, request_handler=QuietHandler,
        )
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            seller = Client(base_url)
            seller.login("http_seller")
            buyers = [Client(base_url) for _number in range(10)]
            for number, buyer in enumerate(buyers):
                buyer.login(f"http_buyer_{number}")
            cases = _run_cases(engine, seller, buyers, product_ids)
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
            engine.dispose()
    return {
        "passed": all(case["passed"] for case in cases), "cases": cases,
        "server": "Real HTTP, threaded Werkzeug, separate clients, on-disk SQLite.",
        "data": "Generated isolated users and products; database removed after verification.",
        "scope": "Five concurrency correctness cases; expected 409 losers are retained.",
        "racing_http_requests": sum(len(case["http"]) for case in cases),
    }


def main():
    """Persist every race outcome and exit unsuccessfully if a correctness check fails."""
    report = verify()
    destination = Path(__file__).resolve().parents[1] / "instance" / "reservation-race-check.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"], "racing_http_requests": report["racing_http_requests"],
        "cases": [{"case": case["case"], "passed": case["passed"],
                   "statuses": [row["status"] for row in case["http"]]}
                  for case in report["cases"]],
    }, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
