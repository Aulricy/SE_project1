"""Role, ownership, provisioning, and approval authorization checks."""

import pytest
from flask import g
from sqlalchemy import func, select
from werkzeug.exceptions import Forbidden, Unauthorized
from werkzeug.security import check_password_hash

from app.database import get_session
from app.models import User, UserRole
from app.services.users import UserService
from tests.helpers import PASSWORD, login_as, post_form


@pytest.mark.parametrize("path", ["/account", "/admin", "/admin/accounts", "/admin/accounts/new"])
def test_guest_is_sent_to_login(app, path):
    """Protected pages request login and preserve a local return path."""
    response = app.test_client().get(path)
    assert response.status_code == 303
    assert "/auth/login?next=" in response.location


@pytest.mark.parametrize("username,path,expected", [
    ("member", "/admin", 403), ("reviewer", "/admin", 200), ("initial", "/admin", 200),
    ("member", "/admin/accounts", 403), ("reviewer", "/admin/accounts", 403),
    ("initial", "/admin/accounts", 200),
    ("member", "/admin/accounts/new", 403), ("reviewer", "/admin/accounts/new", 403),
    ("initial", "/admin/accounts/new", 200),
])
def test_role_access_matrix(app, accounts, username, path, expected):
    """Hidden menus are backed by actual server authorization."""
    assert accounts
    client = app.test_client()
    assert login_as(client, username).status_code == 303
    assert client.get(path).status_code == expected


@pytest.mark.parametrize("username", ["member", "reviewer", None])
def test_direct_admin_creation_is_denied_without_initial_identity(app, accounts, username):
    """A valid CSRF token and forged actor fields cannot replace the required identity."""
    client = app.test_client()
    if username is not None:
        assert login_as(client, username).status_code == 303
    response = post_form(client, "/admin/accounts/new", {
        "username": "forbidden-admin", "password": PASSWORD, "confirm": PASSWORD,
        "approved": "y", "role": "ADMIN", "actor": accounts["initial"],
        "user_id": accounts["initial"], "initialAdminId": accounts["initial"],
    }, csrf_path="/account" if username is not None else "/auth/login")
    assert response.status_code == (303 if username is None else 403)
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(User)) == len(accounts)


def test_initial_admin_creates_an_admin_who_cannot_delegate(app, accounts):
    """The approved account can log in as ADMIN but cannot add more administrators."""
    assert accounts
    client = app.test_client()
    assert login_as(client, "initial").status_code == 303
    response = post_form(client, "/admin/accounts/new", {
        "username": "new-reviewer", "password": PASSWORD, "confirm": PASSWORD, "approved": "y",
    })
    assert response.status_code == 303
    listing = client.get("/admin/accounts").get_data(as_text=True)
    assert "new-reviewer" in listing
    assert PASSWORD not in listing
    newcomer = app.test_client()
    assert login_as(newcomer, "new-reviewer").status_code == 303
    assert newcomer.get("/admin").status_code == 200
    assert newcomer.get("/admin/accounts").status_code == 403
    assert newcomer.get("/admin/accounts/new").status_code == 403


def test_admin_creation_requires_explicit_approval(app, accounts):
    """No account is created when the initial administrator has not confirmed approval."""
    assert accounts
    client = app.test_client()
    assert login_as(client, "initial").status_code == 303
    response = post_form(client, "/admin/accounts/new", {
        "username": "not-approved", "password": PASSWORD, "confirm": PASSWORD,
    })
    assert response.status_code == 400
    with app.app_context():
        assert get_session().scalar(select(User).where(User.username == "not-approved")) is None


@pytest.mark.parametrize("occupied", ["member", "reviewer", "initial"])
def test_occupied_name_cannot_be_promoted_or_overwritten(app, accounts, occupied):
    """Approval creates a new account; it cannot upgrade or reset an existing one."""
    client = app.test_client()
    assert login_as(client, "initial").status_code == 303
    response = post_form(client, "/admin/accounts/new", {
        "username": occupied, "password": "Replacement123!", "confirm": "Replacement123!",
        "approved": "y",
    })
    assert response.status_code == 409
    with app.app_context():
        account = get_session().get(User, accounts[occupied])
        assert check_password_hash(account.password_hash, PASSWORD)
        expected = UserRole.MEMBER if occupied == "member" else UserRole.ADMIN
        assert account.role == expected


def test_initial_admin_cli_is_private_repeatable_and_preserves_members(app):
    """Reserved numbering allows public registration before local admin provisioning."""
    assert post_form(app.test_client(), "/auth/register", {
        "username": "early-member", "password": PASSWORD, "confirm": PASSWORD,
    }).status_code == 303
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["init-admin", "--username", "root"], input=f"{PASSWORD}\n{PASSWORD}\n",
    )
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    second = runner.invoke(
        args=["init-admin", "--username", "replacement"],
        input="DifferentPassword123!\nDifferentPassword123!\n",
    )
    assert second.exit_code != 0
    with app.app_context():
        database = get_session()
        initial = database.get(User, app.config["INITIAL_ADMIN_ID"])
        assert initial.username == "root"
        assert check_password_hash(initial.password_hash, PASSWORD)
        member = database.scalar(select(User).where(User.username == "early-member"))
        assert member.id != initial.id
        assert member.role == UserRole.MEMBER
        assert database.scalar(select(func.count()).select_from(User)) == 2


def test_owner_guard_rejects_cross_account_access_even_for_admin(app, accounts):
    """Shared ownership checks never infer ownership from admin status or supplied IDs."""
    with app.test_request_context():
        with pytest.raises(Unauthorized):
            UserService.require_owner(accounts["member"])
        g.user = get_session().get(User, accounts["member"])
        assert UserService.require_owner(accounts["member"]).id == accounts["member"]
        with pytest.raises(Forbidden):
            UserService.require_owner(accounts["other"])
        g.user = get_session().get(User, accounts["initial"])
        with pytest.raises(Forbidden):
            UserService.require_owner(accounts["member"])
