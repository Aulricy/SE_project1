"""UserService: registration, authentication, and server-enforced permissions."""

import hashlib
import secrets
import unicodedata

from flask import current_app, g, session
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden, Unauthorized
from werkzeug.security import check_password_hash, generate_password_hash

from app.database import get_engine, get_session
from app.models import LoginSession, User, UserRole
from app.models.base import utc_now

_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))


class AccountError(Exception):
    """A safe form error that can be shown without exposing database details."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _validate_credentials(username: str, password: str) -> str:
    """Normalize the name while keeping the password exactly as entered."""
    username = username.strip()
    if not 1 <= len(username) <= 40 or any(
        unicodedata.category(character).startswith("C") for character in username
    ):
        raise AccountError("用户名须为 1–40 个字符，不能包含控制字符。")
    if not 8 <= len(password) <= 128 or password.isspace():
        raise AccountError("密码须为 8–128 个字符，且不能全部为空白。")
    return username


def _token_hash(token: str) -> str:
    """Hash high-entropy session tokens before persistence."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _save_user(username: str, password: str, role: UserRole, user_id: int | None = None) -> User:
    """Rely on the database unique constraint, including for concurrent registration."""
    username = _validate_credentials(username, password)
    account = User(
        id=user_id,
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
    )
    database = get_session()
    database.add(account)
    try:
        database.commit()
    except IntegrityError as error:
        database.rollback()
        raise AccountError("该用户名已被使用，请换一个。", 409) from error
    return account


class UserService:
    """Map the user and permission operations from the experiment-two class design."""

    @staticmethod
    def register(username: str, password: str) -> User:
        """Ordinary registration never accepts a role or user identifier."""
        return _save_user(username, password, UserRole.MEMBER)

    @staticmethod
    def bootstrap_admin(username: str, password: str) -> User:
        """Provision the configured initial administrator through the local CLI only."""
        initial_id = current_app.config["INITIAL_ADMIN_ID"]
        if get_session().get(User, initial_id) is not None:
            raise AccountError("首个管理员已预置；此命令不会覆盖账号或重置密码。", 409)
        return _save_user(username, password, UserRole.ADMIN, user_id=initial_id)

    @staticmethod
    def load_current_user() -> None:
        """Resolve identity and current role from the database, never from form fields."""
        g.user = None
        token = session.get("auth_token")
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return
        # End the authentication read transaction before a business service writes.
        with Session(get_engine()) as database:
            record = database.execute(
                select(User, LoginSession.expires_at)
                .join(LoginSession, LoginSession.user_id == User.id)
                .where(LoginSession.token_hash == _token_hash(token))
            ).first()
            if record is not None and record.expires_at > utc_now():
                g.user = record.User
                return
        session.clear()

    @staticmethod
    def login(username: str, password: str) -> User:
        """Check a salted hash and rotate the current browser's server session."""
        username = username.strip()
        if not 1 <= len(username) <= 40 or not 1 <= len(password) <= 128:
            raise AccountError("用户名或密码错误。", 401)
        with Session(get_engine()) as database:
            account = database.scalar(select(User).where(User.username == username))
            stored_hash = account.password_hash if account is not None else _DUMMY_HASH
            valid = check_password_hash(stored_hash, password)
        if account is None or not valid:
            raise AccountError("用户名或密码错误。", 401)

        database = get_session()
        previous_token = session.get("auth_token")
        if isinstance(previous_token, str):
            database.execute(
                delete(LoginSession).where(LoginSession.token_hash == _token_hash(previous_token))
            )
        database.execute(delete(LoginSession).where(LoginSession.expires_at <= utc_now()))
        token = secrets.token_urlsafe(32)
        database.add(LoginSession(
            token_hash=_token_hash(token),
            user_id=account.id,
            expires_at=utc_now() + current_app.permanent_session_lifetime,
        ))
        database.commit()
        session.clear()
        session.permanent = True
        session["auth_token"] = token
        g.user = account
        return account

    @staticmethod
    def logout() -> None:
        """Revoke the database session so a copied old cookie cannot restore login."""
        token = session.get("auth_token")
        if isinstance(token, str):
            database = get_session()
            database.execute(
                delete(LoginSession).where(LoginSession.token_hash == _token_hash(token))
            )
            database.commit()
        session.clear()
        g.user = None

    @staticmethod
    def require_login() -> User:
        """Return the authenticated actor or request login."""
        account = g.get("user")
        if account is None:
            raise Unauthorized()
        return account

    @staticmethod
    def require_member() -> User:
        """Restrict buying and selling operations to an ordinary account."""
        account = UserService.require_login()
        if account.role != UserRole.MEMBER:
            raise Forbidden()
        return account

    @staticmethod
    def require_admin() -> User:
        """Restrict a business operation to a currently authenticated administrator."""
        account = UserService.require_login()
        if account.role != UserRole.ADMIN:
            raise Forbidden()
        return account

    @staticmethod
    def require_initial_admin() -> User:
        """Only the configured initial admin may approve and set additional admins."""
        account = UserService.require_admin()
        if account.id != current_app.config["INITIAL_ADMIN_ID"]:
            raise Forbidden()
        return account

    @staticmethod
    def require_owner(owner_id: int) -> User:
        """Check ownership independently of menu visibility or account role."""
        account = UserService.require_login()
        if account.id != owner_id:
            raise Forbidden()
        return account

    @staticmethod
    def create_admin(username: str, password: str) -> User:
        """Create a new ADMIN; never upgrade or overwrite an existing ordinary user."""
        UserService.require_initial_admin()
        return _save_user(username, password, UserRole.ADMIN)

    @staticmethod
    def list_admins() -> list[User]:
        """Limit the administrator account list to the initial administrator."""
        UserService.require_initial_admin()
        return list(get_session().scalars(
            select(User).where(User.role == UserRole.ADMIN).order_by(User.id)
        ))
