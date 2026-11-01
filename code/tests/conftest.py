"""Isolated application and database fixtures for integration checks."""

from pathlib import Path

import pytest
from flask import g

from app import create_app
from app.services.users import UserService
from tests.helpers import PASSWORD, login_as
from tests.product_helpers import create_product, product_action


@pytest.fixture(name="app")
def app_fixture(tmp_path: Path):
    """Give every test its own on-disk database and engine."""
    application = create_app({
        "TESTING": True,
        "SECRET_KEY": "isolated-test-secret",
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
    })
    result = application.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.exception
    yield application
    application.extensions["database_engine"].dispose()


@pytest.fixture(name="accounts")
def accounts_fixture(app):
    """Seed identities through services; passwords are fictitious test-only values."""
    with app.app_context():
        initial = UserService.bootstrap_admin("initial", PASSWORD)
        member = UserService.register("member", PASSWORD)
        other = UserService.register("other", PASSWORD)
        g.user = initial
        reviewer = UserService.create_admin("reviewer", PASSWORD)
        return {
            "initial": initial.id, "member": member.id,
            "other": other.id, "reviewer": reviewer.id,
        }

@pytest.fixture(name="shop")
def shop_fixture(app, accounts):
    """Use separate authenticated clients for the seller, another owner, and a reviewer."""
    clients = {key: app.test_client() for key in ("seller", "other", "admin")}
    for key, username in (("seller", "member"), ("other", "other"), ("admin", "reviewer")):
        assert login_as(clients[key], username).status_code == 303
    return {**clients, "ids": accounts}

@pytest.fixture(name="trade")
def trade_fixture(app, shop):
    """Publish one product and provide an unrelated ordinary account for permission checks."""
    with app.app_context():
        UserService.register("outsider", PASSWORD)
    product_id = create_product(shop["seller"], intent="submit")
    assert product_action(shop["admin"], product_id, 1, "approve").status_code == 303
    return {**shop, "buyer": shop["other"], "product_id": product_id}
