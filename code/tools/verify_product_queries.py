"""Measure REQ-017 using isolated data and real HTTP requests, including all failures."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import platform
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter
from urllib.error import URLError
from urllib.request import urlopen

from sqlalchemy.orm import Session
from werkzeug.serving import WSGIRequestHandler, make_server

from app import create_app
from app.database import get_engine
from app.models import Product, ProductCategory, ProductStatus
from app.services.users import UserService


class QuietHandler(WSGIRequestHandler):
    """Keep generated load out of the developer's regular request log."""

    def log_request(self, code="-", size="-"):
        """Each request outcome is stored in the JSON result instead."""


def measure() -> dict:
    """Run ten concurrent clients with ten list requests each against 1,000 products."""
    parent = Path(__file__).resolve().parents[1] / "instance"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="query-check-", dir=parent) as temporary:
        workspace = Path(temporary).resolve()
        assert workspace.is_relative_to(parent.resolve())
        application = create_app({
            "SECRET_KEY": "isolated-performance-test",
            "DATABASE_URL": "sqlite:///" + (workspace / "queries.sqlite3").as_posix(),
            "UPLOAD_FOLDER": str(workspace / "uploads"),
        })
        result = application.test_cli_runner().invoke(args=["init-db"])
        if result.exit_code != 0:
            raise RuntimeError("Performance fixture initialization failed") from result.exception
        with application.app_context():
            seller = UserService.register("performance_seller", "TestOnlyPassword123!")
            with Session(get_engine()) as database, database.begin():
                database.add_all([
                    Product(
                        seller_id=seller.id, name=f"测试键盘 {index}", price_cents=1000 + index,
                        category=ProductCategory.DIGITAL, description="性能测试生成数据",
                        status=ProductStatus.PUBLISHED, published_at=datetime(2026, 10, 31),
                    ) for index in range(1000)
                ])
        server = make_server(
            "127.0.0.1", 0, application, threaded=True, request_handler=QuietHandler,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()


        def client_requests(client_id):
            """Retain every request, including errors, timeouts and slow responses."""
            url = (
                f"http://127.0.0.1:{server.server_port}/products?"
                "keyword=%E9%94%AE%E7%9B%98&category=DIGITAL&min_price=10&max_price=20&page=1"
            )
            records = []
            for number in range(10):
                start = perf_counter()
                status, valid, error = None, False, None
                try:
                    with urlopen(url, timeout=5) as response:
                        status = response.status
                        valid = "共 1000 件商品" in response.read().decode("utf-8")
                except (URLError, OSError, TimeoutError) as exception:
                    error = type(exception).__name__
                elapsed = perf_counter() - start
                records.append({
                    "client": client_id, "request": number + 1, "status": status,
                    "seconds": round(elapsed, 6), "expected_content": valid,
                    "passed": status == 200 and valid and elapsed <= 2, "error": error,
                })
            return records

        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                batches = list(executor.map(client_requests, range(1, 11)))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            application.extensions["database_engine"].dispose()
    records = [record for batch in batches for record in batch]
    passed = sum(record["passed"] for record in records)
    durations = sorted(record["seconds"] for record in records)
    return {
        "requirement": "REQ-017", "products": 1000, "concurrent_clients": 10,
        "requests": len(records), "passed_requests": passed,
        "criterion": "All requests count; at least 95/100 return valid HTTP 200 within 2 seconds.",
        "passed": len(records) == 100 and passed >= 95,
        "p95_seconds": durations[94], "max_seconds": durations[-1],
        "machine": {
            "os": platform.platform(), "cpu": platform.processor(),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        },
        "server": "Local threaded Werkzeug development server; isolated SQLite file.",
        "scope": ("HTML list latency includes HTTP, SQL and template rendering; "
                  "excludes image downloads."),
        "results": records,
    }


def main() -> None:
    """Persist measured outcomes without touching normal application data."""
    report = measure()
    destination = Path(__file__).resolve().parents[1] / "instance" / "product-query-check.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"},
                     ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
