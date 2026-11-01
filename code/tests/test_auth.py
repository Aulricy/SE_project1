"""Registration and login checks through CSRF-protected HTTP forms."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select
from werkzeug.security import check_password_hash

from app import create_app
from app.database import get_session
from app.models import LoginSession, User, UserRole
from tests.helpers import PASSWORD, login_as, post_form, token_for


def test_registration_ignores_forged_role_and_identifier(app):
    """Public input cannot grant ADMIN or claim the reserved initial-admin identity."""
    response = post_form(app.test_client(), "/auth/register", {
        "username": " alice ", "password": PASSWORD, "confirm": PASSWORD,
        "role": "ADMIN", "id": 1, "initialAdminId": 99,
    })
    assert response.status_code == 303
    assert "/auth/login" in response.location
    with app.app_context():
        account = get_session().scalar(select(User).where(User.username == "alice"))
        assert account.role == UserRole.MEMBER
        assert account.id > app.config["INITIAL_ADMIN_ID"]
        assert PASSWORD not in account.password_hash
        assert check_password_hash(account.password_hash, PASSWORD)
    assert PASSWORD.encode() not in response.data


@pytest.mark.parametrize("changes", [
    {"username": " "}, {"username": "x" * 41}, {"username": "bad\x00name"},
    {"password": "short", "confirm": "short"},
    {"password": " " * 8, "confirm": " " * 8},
    {"confirm": "DifferentPassword"}, {"confirm": ""},
])
def test_invalid_registration_does_not_create_account(app, changes):
    """Reject missing, oversized, control-character, and mismatching input."""
    response = post_form(app.test_client(), "/auth/register", {
        "username": "alice", "password": PASSWORD, "confirm": PASSWORD, **changes,
    })
    assert response.status_code == 400
    assert PASSWORD.encode() not in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(User)) == 0


def test_duplicate_name_keeps_original_password_and_role(app):
    """A duplicate registration must not overwrite or upgrade an existing account."""
    client = app.test_client()
    payload = {"username": "alice", "password": PASSWORD, "confirm": PASSWORD}
    assert post_form(client, "/auth/register", payload).status_code == 303
    response = post_form(client, "/auth/register", {
        "username": " alice ", "password": "Replacement123!", "confirm": "Replacement123!",
        "role": "ADMIN",
    })
    assert response.status_code == 409
    assert "该用户名已被使用" in response.get_data(as_text=True)
    with app.app_context():
        account = get_session().scalar(select(User))
        assert account.role == UserRole.MEMBER
        assert check_password_hash(account.password_hash, PASSWORD)
        assert get_session().scalar(select(func.count()).select_from(User)) == 1


def test_same_password_has_different_salts(app):
    """Password storage uses a fresh salt for every account."""
    for username in ("alice", "bob"):
        assert post_form(app.test_client(), "/auth/register", {
            "username": username, "password": PASSWORD, "confirm": PASSWORD,
        }).status_code == 303
    with app.app_context():
        hashes = list(get_session().scalars(select(User.password_hash)))
        assert len(set(hashes)) == 2
        assert all(check_password_hash(value, PASSWORD) for value in hashes)


def test_concurrent_duplicate_registration_has_one_winner(app):
    """Two independent clients racing for a username cannot create two users."""
    barrier = Barrier(2)

    def register_once(_number):
        """Synchronize two valid forms immediately before their POST requests."""
        client = app.test_client()
        token = token_for(client, "/auth/register")
        barrier.wait(timeout=5)
        return client.post("/auth/register", data={
            "csrf_token": token, "username": "racing",
            "password": PASSWORD, "confirm": PASSWORD,
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(register_once, range(2)))
    assert sorted(statuses) == [303, 409]
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("username,role", [
    ("member", "普通用户"), ("reviewer", "管理员"), ("initial", "首个管理员"),
])
def test_both_roles_use_the_same_login(app, accounts, username, role):
    """A database-derived identity, rather than a selected role, determines the UI."""
    assert accounts
    client = app.test_client()
    assert login_as(client, username, role="ADMIN", user_id=1).status_code == 303
    response = client.get("/account")
    assert response.status_code == 200
    assert role in response.get_data(as_text=True)
    assert PASSWORD.encode() not in response.data


@pytest.mark.parametrize("username", ["member", "missing"])
def test_wrong_password_and_unknown_name_have_same_error(app, accounts, username):
    """Failed login has no session side effect and does not identify existing usernames."""
    assert accounts
    client = app.test_client()
    response = post_form(client, "/auth/login", {
        "username": username, "password": "WrongPassword123!",
    })
    assert response.status_code == 401
    assert "用户名或密码错误" in response.get_data(as_text=True)
    assert "WrongPassword123!" not in response.get_data(as_text=True)
    assert client.get("/account").status_code == 303
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(LoginSession)) == 0


def test_data_and_login_survive_application_restart(app):
    """Recreating the app preserves users and an unexpired server-side login."""
    client = app.test_client()
    assert post_form(client, "/auth/register", {
        "username": "persistent", "password": PASSWORD, "confirm": PASSWORD,
    }).status_code == 303
    assert login_as(client, "persistent").status_code == 303
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": app.config["SECRET_KEY"],
        "DATABASE_URL": app.config["DATABASE_URL"],
    })
    try:
        restarted_client = restarted.test_client()
        restarted_client.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie.value)
        assert restarted_client.get("/account").status_code == 200
        assert "persistent" in restarted_client.get("/account").get_data(as_text=True)
    finally:
        restarted.extensions["database_engine"].dispose()
