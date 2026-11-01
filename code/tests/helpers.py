"""Small HTTP helpers that keep real CSRF protection enabled in tests."""

from html import unescape
import re

PASSWORD = "TestPassword123!"


def token_for(client, path: str) -> str:
    """Read a genuine token issued to the current client by an HTML form."""
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
    assert match is not None
    return unescape(match.group(1))


def post_form(client, path: str, data: dict, csrf_path: str | None = None):
    """Submit a form with the same browser's CSRF cookie and token."""
    token = token_for(client, csrf_path or path)
    return client.post(path, data={"csrf_token": token, **data})


def login_as(client, username: str, **extra):
    """Log in through the shared public login route."""
    return post_form(client, "/auth/login", {
        "username": username, "password": PASSWORD, **extra,
    })
