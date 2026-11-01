"""End-to-end acceptance over real HTTP, database integrity and a normal process restart."""

from contextlib import closing
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from uuid import uuid4

from PIL import Image

from app import create_app
from app.models import OperationType
from app.services.users import UserService
from tools.acceptance_http import AcceptanceClient, FormParser, ProcessServer

PASSWORD = "HttpTestPassword123!"
CONTACT = "测试联系 fixture@example.invalid；线下当面交付，不进行线上付款。"


class AcceptanceFlow:
    """Exercise all five modules through real roles, form controls and HTTP requests."""

    def __init__(self, server, database):
        self.server = server
        self.database = database
        self.clients = {name: AcceptanceClient(server.base_url)
                        for name in ("initial", "reviewer", "seller", "buyer", "other")}
        self.steps = []
        self.sent = {}
        self.ids = {}
        self.original_image = self._image_bytes()

    @staticmethod
    def _image_bytes():
        stream = BytesIO()
        Image.new("RGB", (24, 24), (35, 110, 70)).save(stream, format="PNG")
        return stream.getvalue()

    def query(self, sql, parameters=()):
        """Inspect committed evidence only; business changes always travel through HTTP."""
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    def digest(self):
        """Compare complete persisted values without putting credential hashes in the report."""
        tables = ("users", "login_sessions", "products", "reservations",
                  "operation_requests", "operation_logs")
        payload = {table: self.query(f"SELECT * FROM {table} ORDER BY rowid") for table in tables}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def expect(self, label, response, expected=200, contains=()):
        """Keep actual success and expected rejection outcomes; never discard failures."""
        passed = response[0] == expected and all(value in response[1] for value in contains)
        self.steps.append({"check": label, "status": response[0], "expected": expected,
                           "passed": passed})
        assert passed, f"{label}: expected {expected}, got {response[0]} or missing content"
        return response

    def form(self, actor, target, values=None, **options):
        """Submit the actual page controls and retain their identity for replay checks."""
        expected = options.pop("expected", 303)
        response, fields = self.clients[actor].submit(target, values, **options)
        self.sent[actor, target] = fields
        return self.expect(actor + " POST " + target, response, expected)

    def direct(self, actor, target, values, expected):
        """Negative/stale commands deliberately bypass buttons but still use valid CSRF."""
        client = self.clients[actor]
        status, html, _location = client.request("/account")
        assert status == 200
        token = FormParser(html).forms[0].fields["csrf_token"]
        return self.expect(actor + " direct " + target, client.post_fields(
            target, {"csrf_token": token, **values},
        ), expected)

    @staticmethod
    def identifier(response):
        """The successful route supplies the new object's identity."""
        assert response[0] == 303
        return int(response[2].rsplit("/", 1)[1])

    def product_action(self, actor, pid, operation, reason=""):
        """Use the authorized product detail form's current version."""
        return self.form(actor, f"/products/{pid}/actions/{operation}", {"reason": reason},
                         source=f"/products/{pid}")

    def trade_action(self, actor, rid, operation, reason=""):
        """Use the participant's original reservation controls."""
        return self.form(actor, f"/reservations/{rid}/actions/{operation}", {"reason": reason},
                         source=f"/reservations/{rid}")

    @staticmethod
    def product_values(name, price):
        """Deliberately fictitious contact and product data, never real trading records."""
        return {"name": name, "category": "DIGITAL", "price": price, "description": CONTACT}

    def publish(self, name, price):
        """Create a complete submission and approve it through separate HTTP identities."""
        pid = self.identifier(self.form("seller", "/products/new", {
            **self.product_values(name, price), "intent": "submit",
        }))
        self.product_action("reviewer", pid, "approve")
        return pid

    def accounts(self):
        """Register ordinary users and let only the initial admin create a reviewer."""
        self.clients["initial"].login("accept_initial")
        for actor in ("seller", "buyer", "other"):
            self.form(actor, "/auth/register", {
                "username": "accept_" + actor, "password": PASSWORD, "confirm": PASSWORD,
                "role": "ADMIN", "id": "1",
            })
            self.clients[actor].login("accept_" + actor)
        self.form("initial", "/admin/accounts/new", {
            "username": "accept_reviewer", "password": PASSWORD, "confirm": PASSWORD,
            "approved": "y",
        })
        self.clients["reviewer"].login("accept_reviewer")
        self.expect("reviewer cannot create more admins",
                    self.clients["reviewer"].request("/admin/accounts/new"), 403)
        self.expect("member cannot query admin logs",
                    self.clients["buyer"].request("/admin/logs"), 403)
        members = self.query("SELECT role, password_hash FROM users WHERE role='MEMBER'")
        assert len(members) == 3 and len({row["password_hash"] for row in members}) == 3
        assert all(row["password_hash"].startswith("scrypt:") for row in members)

    def publication(self):
        """Validate drafts, image upload, withdrawal, rejection, correction and publication."""
        pid = self.identifier(self.form("seller", "/products/new", {
            "name": "", "category": "", "price": "", "description": "",
        }))
        self.ids["completed_image_product"] = pid
        visitor = AcceptanceClient(self.server.base_url)
        self.expect("draft stays private", visitor.request(f"/products/{pid}"), 404)
        before = self.digest()
        self.form("seller", f"/products/{pid}/edit", {
            **self.product_values("验收图书", "-1"), "intent": "submit",
        }, expected=400)
        assert before == self.digest()
        self.form("seller", f"/products/{pid}/edit", {
            **self.product_values("验收图书", "10.50"), "intent": "submit",
        }, image=self.original_image)
        self.product_action("seller", pid, "withdraw")
        self.product_action("seller", pid, "submit")
        self.product_action("reviewer", pid, "reject", "请补充说明后重新审核")
        self.form("seller", f"/products/{pid}/edit", {
            **self.product_values("验收图书", "10.50"), "intent": "submit",
        })
        self.product_action("reviewer", pid, "approve")
        self.expect("published contact visible without login", visitor.request(f"/products/{pid}"),
                    contains=(CONTACT, "验收图书"))
        status, mime, content = visitor.image(f"/products/{pid}/image")
        assert status == 200 and mime == "image/png" and content == self.original_image

    def complete(self, pid, actor):
        """The seller confirms and the matching buyer completes the original reservation."""
        rid = self.identifier(self.form(
            actor, f"/products/{pid}/reserve", source=f"/products/{pid}",
        ))
        self.product_guard(pid, rid)
        self.trade_action("seller", rid, "confirm")
        self.trade_action(actor, rid, "complete")
        target = f"/reservations/{rid}/actions/complete"
        before = self.digest()
        self.expect("completion replay is read-only",
                    self.clients[actor].post_fields(target, self.sent[actor, target]), 303)
        assert before == self.digest()
        return rid

    def product_guard(self, pid, rid):
        """An effective reservation prevents changing the occupied product's description."""
        before = self.digest()
        version = self.query("SELECT version FROM products WHERE id=?", (pid,))[0]["version"]
        self.direct("seller", f"/products/{pid}/edit", {
            **self.product_values("不得修改", "0.01"), "version": version,
        }, 409)
        self.expect("unrelated buyer cannot view reservation",
                    self.clients["other"].request(f"/reservations/{rid}"),
                    404)
        assert before == self.digest()

    def cancellation_and_moderation(self):
        """Cancel, reprice, rebook, block confirmed trade, restore and re-review without revival."""
        pid = self.publish("验收键盘", "20.00")
        self.ids["moderated_product"] = pid
        self.product_action("seller", pid, "take-down")
        self.product_action("seller", pid, "return-draft")
        self.product_action("seller", pid, "submit")
        self.product_action("reviewer", pid, "approve")
        reserve_path = f"/products/{pid}/reserve"
        old_rid = self.identifier(self.form("buyer", reserve_path, source=f"/products/{pid}"))
        old_reserve = self.sent["buyer", reserve_path]
        self.trade_action("buyer", old_rid, "cancel", "先取消，保留原预约价格")
        cancel_path = f"/reservations/{old_rid}/actions/cancel"
        old_cancel = self.sent["buyer", cancel_path]
        self.product_action("seller", pid, "return-draft")
        self.form("seller", f"/products/{pid}/edit", {
            **self.product_values("改价后的验收键盘", "30.00"), "intent": "submit",
        })
        self.product_action("reviewer", pid, "approve")
        blocked_rid = self.identifier(self.form("other", reserve_path, source=f"/products/{pid}"))
        self.ids["blocked_reservation"] = blocked_rid
        self.trade_action("seller", blocked_rid, "confirm")
        confirm_path = f"/reservations/{blocked_rid}/actions/confirm"
        old_confirm = self.sent["seller", confirm_path]
        self.product_action("reviewer", pid, "block", "验收生成原因：描述违规，整改后重新审核")
        self.expect("buyer sees linked cancellation", self.clients["other"].request(
            f"/reservations/{blocked_rid}"), contains=("已屏蔽", "已取消", "描述违规"))
        self.product_action("reviewer", pid, "restore")
        self.expect("restore keeps old reservation cancelled", self.clients["other"].request(
            f"/reservations/{blocked_rid}"), contains=("草稿", "已取消", "描述违规"))
        self.product_action("seller", pid, "submit")
        self.product_action("reviewer", pid, "approve")
        self.ids["active_reservation"] = self.identifier(self.form(
            "other", reserve_path, source=f"/products/{pid}",
        ))
        before = self.digest()
        for actor, path, fields in (("buyer", reserve_path, old_reserve),
                                    ("buyer", cancel_path, old_cancel),
                                    ("seller", confirm_path, old_confirm)):
            self.expect("old request never changes new occupant",
                        self.clients[actor].post_fields(path, fields), 303)
        assert before == self.digest()
        prices = self.query("SELECT reserved_price_cents FROM reservations WHERE product_id=? "
                            "ORDER BY id", (pid,))
        assert [row["reserved_price_cents"] for row in prices] == [2000, 3000, 3000]

    def queries(self):
        """Cross-check completion accounting and a composite moderation log from real actions."""
        query = urlencode({"start": "2026-01-01", "end": "2027-12-31"})
        self.expect("two completed trades total 30.50", self.clients["reviewer"].request(
            "/admin/statistics?" + query), contains=("¥30.50", 'id="completed-count">2 '))
        query = urlencode({"product_id": self.ids["moderated_product"],
                           "reservation_id": self.ids["blocked_reservation"],
                           "operator": "accept_reviewer", "type": "BLOCK_PRODUCT"})
        result = self.expect("combined moderation audit query", self.clients["reviewer"].request(
            "/admin/logs?" + query), contains=("共 1 条日志",))
        log_id = re.search(r'href="/admin/logs/([0-9]+)"', result[1]).group(1)
        self.expect("audit stores both transitions", self.clients["reviewer"].request(
            f"/admin/logs/{log_id}"), contains=("已确认 → 已屏蔽", "已确认 → 已取消", "描述违规"))
        before = self.digest()
        completed = self.ids["completed_image_product"]
        self.direct("reviewer", f"/products/{completed}/actions/block", {
            "version": self.query("SELECT version FROM products WHERE id=?", (completed,))[0][
                "version"], "reason": "已完成交易不得屏蔽",
        }, 409)
        assert before == self.digest()
        kinds = {row["operation_type"] for row in self.query(
            "SELECT operation_type FROM operation_logs",
        )}
        assert kinds == {kind.value for kind in OperationType}

    def restart(self):
        """Normally stop the process; re-use cookies, keys, database and files in a new process."""
        before = self.digest()
        self.server.stop()
        self.server.start()
        for client in self.clients.values():
            client.base_url = self.server.base_url
        assert self.server.pids[0] != self.server.pids[1] and before == self.digest()
        for actor in self.clients:
            self.expect("persisted login after process restart: " + actor,
                        self.clients[actor].request("/account"))
        pid = self.ids["completed_image_product"]
        rid = self.ids["completed_image_reservation"]
        status, mime, content = self.clients["buyer"].image(f"/reservations/{rid}/image")
        assert status == 200 and mime == "image/png" and content == self.original_image
        self.expect("completed product stays private",
                    AcceptanceClient(self.server.base_url).request(
            f"/products/{pid}"), 404)
        self.queries()
        assert before == self.digest()

    def integrity(self):
        """Foreign keys, physical SQLite integrity, state pairs and snapshot counts match."""
        assert self.query("PRAGMA foreign_key_check") == []
        assert list(self.query("PRAGMA integrity_check")[0].values()) == ["ok"]
        products = self.query("SELECT id,status FROM products ORDER BY id")
        for product in products:
            active = self.query("SELECT status FROM reservations WHERE product_id=? AND "
                                "status IN ('PENDING_CONFIRMATION','CONFIRMED')", (product["id"],))
            expected = {"RESERVED": "PENDING_CONFIRMATION", "CONFIRMED": "CONFIRMED"}.get(
                product["status"],
            )
            assert ([row["status"] for row in active] == [expected]) if expected else not active
        assert len(products) == 3
        assert len(self.query("SELECT id FROM reservations")) == 5
        return {"products": products, "reservations": self.query(
            "SELECT id,product_id,status,reserved_price_cents FROM reservations ORDER BY id"),
            "foreign_keys_valid": True, "sqlite_integrity": "ok", "state_pairs_valid": True}

    def run(self):
        """Exercise every business event through one continuous history."""
        self.accounts()
        self.publication()
        self.ids["completed_image_reservation"] = self.complete(
            self.ids["completed_image_product"], "buyer",
        )
        self.cancellation_and_moderation()
        self.complete(self.publish("验收台灯", "20.00"), "buyer")
        self.queries()
        self.restart()
        result = self.integrity()
        old_cookie = self.clients["buyer"].clone()
        self.form("buyer", "/auth/logout", source="/account")
        self.expect("revoked cookie cannot restore login", old_cookie.request("/account"), 303)
        return result


def verify():
    """Create only an initial fake admin locally; all subsequent business writes use HTTP."""
    parent = Path(__file__).resolve().parents[1] / "instance"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="system-acceptance-", dir=parent) as temporary:
        workspace = Path(temporary).resolve()
        assert workspace.is_relative_to(parent.resolve())
        database = workspace / "system.sqlite3"
        config = {"SECRET_KEY": uuid4().hex, "SESSION_COOKIE_NAME": "system_acceptance_session",
                  "DATABASE_URL": "sqlite:///" + database.as_posix(),
                  "UPLOAD_FOLDER": str(workspace / "uploads")}
        application = create_app(config)
        assert application.test_cli_runner().invoke(args=["init-db"]).exit_code == 0
        with application.app_context():
            UserService.bootstrap_admin("accept_initial", PASSWORD)
        application.extensions["database_engine"].dispose()
        with ProcessServer(config) as server:
            flow = AcceptanceFlow(server, database)
            result = flow.run()
        return {"passed": True, "checks": flow.steps, "check_count": len(flow.steps),
                "process_ids": server.pids, "normal_process_restart": True, "database": result,
                "scope": "Real HTTP and multipart upload; generated data; temporary DB removed.",
                "everyday_data_untouched": True}


def main():
    """Save reproducible local evidence and fail the command if a check fails."""
    report = verify()
    destination = Path(__file__).resolve().parents[1] / "instance" / "system-acceptance.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "checks"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
