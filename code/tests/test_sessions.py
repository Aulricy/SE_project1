"""Session revocation, expiry, role refresh, CSRF, and redirect boundary checks."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.database import get_session
from app.models import LoginSession, User, UserRole
from app.models.base import utc_now
from tests.helpers import PASSWORD, login_as, post_form


def test_logout_revokes_copied_cookie_and_rejects_get(app, accounts):
    """A captured pre-logout cookie must not restore an invalidated server session."""
    assert accounts
    client = app.test_client()
    assert login_as(client, "member").status_code == 303
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    assert client.get("/auth/logout").status_code == 405
    assert post_form(client, "/auth/logout", {}, csrf_path="/account").status_code == 303
    replay = app.test_client()
    replay.set_cookie(app.config["SESSION_COOKIE_NAME"], cookie.value)
    assert replay.get("/account").status_code == 303
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(LoginSession)) == 0


def test_expired_server_session_is_rejected(app, accounts):
    """Database expiry takes precedence over a still-signed browser cookie."""
    assert accounts
    client = app.test_client()
    assert login_as(client, "member").status_code == 303
    with app.app_context():
        database = get_session()
        record = database.scalar(select(LoginSession))
        record.expires_at = utc_now() - timedelta(seconds=1)
        database.commit()
    assert client.get("/account").status_code == 303


def test_role_is_reloaded_from_database_for_every_request(app, accounts):
    """Removing ADMIN in persistent data immediately removes the next request's access."""
    client = app.test_client()
    assert login_as(client, "reviewer").status_code == 303
    assert client.get("/admin").status_code == 200
    with app.app_context():
        database = get_session()
        database.get(User, accounts["reviewer"]).role = UserRole.MEMBER
        database.commit()
    assert client.get("/admin").status_code == 403


def test_cookie_identity_fields_and_query_ids_cannot_impersonate(app, accounts):
    """Even correctly signed extra cookie fields are not authoritative identity input."""
    client = app.test_client()
    assert login_as(client, "member").status_code == 303
    with client.session_transaction() as cookie:
        cookie["role"] = "ADMIN"
        cookie["user_id"] = accounts["initial"]
        cookie["initialAdminId"] = accounts["initial"]
    assert client.get("/admin?role=ADMIN&user_id=1").status_code == 403
    response = client.get(f"/account?user_id={accounts['other']}")
    assert response.status_code == 200
    assert "<dd>member</dd>" in response.get_data(as_text=True)


@pytest.mark.parametrize("bad_token", [None, "forged-csrf-token"])
def test_csrf_blocks_registration_and_login_without_data_changes(app, bad_token):
    """Public forms remain protected from cross-site submissions."""
    client = app.test_client()
    data = {"username": "csrf-user", "password": PASSWORD, "confirm": PASSWORD}
    if bad_token is not None:
        data["csrf_token"] = bad_token
    for path in ("/auth/register", "/auth/login"):
        response = client.post(path, data=data)
        assert response.status_code == 400
        assert PASSWORD.encode() not in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(User)) == 0
        assert get_session().scalar(select(func.count()).select_from(LoginSession)) == 0


def test_csrf_blocks_authenticated_writes(app, accounts):
    """A missing CSRF token cannot create administrators or revoke a user's login."""
    client = app.test_client()
    assert login_as(client, "initial").status_code == 303
    assert client.post("/admin/accounts/new", data={
        "username": "csrf-admin", "password": PASSWORD, "confirm": PASSWORD, "approved": "y",
    }).status_code == 400
    assert client.post("/auth/logout").status_code == 400
    assert client.get("/account").status_code == 200
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(User)) == len(accounts)


@pytest.mark.parametrize("destination", [
    "https://example.com/", "//example.com/", "/\\example.com/",
    "/%5cexample.com/", "/%2fexample.com/", "/\r\nLocation: https://example.com",
])
def test_login_rejects_external_or_ambiguous_return_paths(app, accounts, destination):
    """Return URLs cannot turn successful login into an external redirect."""
    assert accounts
    client = app.test_client()
    response = login_as(client, "member", next=destination)
    assert response.status_code == 303
    assert response.location == "/account"


def test_login_returns_to_a_local_authorized_page(app, accounts):
    """A legitimate local destination is retained across the login form."""
    assert accounts
    response = login_as(app.test_client(), "initial", next="/admin/accounts")
    assert response.status_code == 303
    assert response.location == "/admin/accounts"


def test_session_cookie_contains_only_opaque_authentication_and_csrf_data(app, accounts):
    """Browser state contains neither credentials nor authoritative roles."""
    assert accounts
    client = app.test_client()
    assert login_as(client, "member").status_code == 303
    with client.session_transaction() as cookie:
        assert "auth_token" in cookie
        assert "role" not in cookie
        assert "user_id" not in cookie
        assert "password" not in cookie
        token = cookie["auth_token"]
    with app.app_context():
        record = get_session().scalar(select(LoginSession))
        assert token != record.token_hash
        assert len(record.token_hash) == 64
