"""Account pages and reusable, server-checked access boundaries."""

from datetime import UTC, timedelta, timezone
from urllib.parse import unquote, urlsplit

from flask import (
    Blueprint, Flask, current_app, flash, g, redirect, render_template, request, url_for,
)
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import Forbidden, RequestEntityTooLarge, Unauthorized

from app.cli import init_admin_command
from app.forms import AdminAccountForm, LoginForm, RegistrationForm
from app.models import UserRole
from app.services.users import AccountError, UserService

blueprint = Blueprint("auth", __name__)


def _safe_destination(value: str | None) -> str:
    """Permit local paths while rejecting schemes, network paths, and backslashes."""
    if value:
        decoded = unquote(value)
        if all(
            path.startswith("/") and not path.startswith("//") and "\\" not in path
            and not any(ord(character) < 32 for character in path)
            and not urlsplit(path).netloc and not urlsplit(path).scheme
            for path in (value, decoded)
        ):
            return value
    return url_for("auth.account")


@blueprint.route("/auth/register", methods=["GET", "POST"])
def register():
    """Create a MEMBER and return to the common login page."""
    if g.get("user") is not None:
        return redirect(url_for("auth.account"), code=303)
    form = RegistrationForm()
    message = None
    status = 400 if request.method == "POST" else 200
    if form.validate_on_submit():
        try:
            account_record = UserService.register(form.username.data, form.password.data)
        except AccountError as error:
            message, status = str(error), error.status_code
        else:
            flash("注册成功，请使用新账号登录。", "success")
            return redirect(url_for("auth.login", username=account_record.username), code=303)
    return render_template("auth/register.html", form=form, form_error=message), status


@blueprint.route("/auth/login", methods=["GET", "POST"])
def login():
    """Authenticate both roles without accepting a client-selected role."""
    if g.get("user") is not None:
        return redirect(url_for("auth.account"), code=303)
    form = LoginForm()
    if request.method == "GET":
        form.username.data = request.args.get("username", "")
    destination = _safe_destination(request.values.get("next"))
    message = None
    status = 400 if request.method == "POST" else 200
    if form.validate_on_submit():
        try:
            UserService.login(form.username.data, form.password.data)
        except AccountError as error:
            message, status = str(error), error.status_code
        else:
            flash("登录成功。", "success")
            return redirect(destination, code=303)
    return render_template(
        "auth/login.html", form=form, form_error=message, destination=destination,
    ), status


@blueprint.post("/auth/logout")
def logout():
    """Logout is a CSRF-protected POST and revokes the persisted session."""
    UserService.logout()
    flash("已退出登录。", "success")
    return redirect(url_for("auth.login"), code=303)


@blueprint.get("/account")
def account():
    """Only show the account selected by the authenticated server-side session."""
    UserService.require_login()
    return render_template("auth/account.html")


@blueprint.get("/admin")
def admin_dashboard():
    """Shared administrator entry, with authorization checked on the server."""
    UserService.require_admin()
    return render_template("admin/dashboard.html")


@blueprint.get("/admin/accounts")
def admin_accounts():
    """The initial administrator can inspect approved administrator accounts."""
    accounts = UserService.list_admins()
    return render_template("admin/accounts.html", accounts=accounts)


@blueprint.route("/admin/accounts/new", methods=["GET", "POST"])
def new_admin():
    """Require initial-admin identity before reading or processing any account input."""
    UserService.require_initial_admin()
    form = AdminAccountForm()
    message = None
    status = 400 if request.method == "POST" else 200
    if form.validate_on_submit():
        try:
            account_record = UserService.create_admin(form.username.data, form.password.data)
        except AccountError as error:
            message, status = str(error), error.status_code
        else:
            flash(f"已批准并创建管理员账号：{account_record.username}。", "success")
            return redirect(url_for("auth.admin_accounts"), code=303)
    return render_template("admin/new_account.html", form=form, form_error=message), status


def _identity_context() -> dict:
    """Expose presentation flags computed exclusively from the authenticated user."""
    current_user = g.get("user")
    initial = (
        current_user is not None and current_user.role == UserRole.ADMIN
        and current_user.id == current_app.config["INITIAL_ADMIN_ID"]
    )
    return {"current_user": current_user, "is_initial_admin": initial, "UserRole": UserRole}


def _login_required(_error):
    """Preserve only a local destination when a protected route requires login."""
    flash("请先登录后再操作。", "info")
    return redirect(url_for("auth.login", next=request.path), code=303)


def _forbidden(_error):
    """Use the same response for all denied role and ownership checks."""
    return render_template(
        "error.html", title="没有访问权限", message="当前账号无权访问此页面或执行此操作。",
    ), 403


def _csrf_failed(_error):
    """Do not reflect tokens or submitted password fields in an expired-form response."""
    return render_template(
        "error.html", title="操作未完成", message="页面已失效或校验未通过，请刷新页面后重试。",
    ), 400


def _disable_private_caching(response):
    """Keep login forms and private account pages out of browser caches."""
    if request.endpoint not in {"static", "main.health"}:
        response.headers["Cache-Control"] = "no-store"
    return response


def _beijing_time(value, seconds=False):
    """Display the design's Beijing timestamps without requiring an OS timezone database."""
    return value.replace(tzinfo=UTC).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def init_app(app: Flask) -> None:
    """Register authentication hooks, pages, errors, CLI, and template helpers."""
    app.before_request(UserService.load_current_user)
    app.context_processor(_identity_context)
    app.register_error_handler(Unauthorized, _login_required)
    app.register_error_handler(Forbidden, _forbidden)
    app.register_error_handler(CSRFError, _csrf_failed)
    app.register_error_handler(RequestEntityTooLarge, lambda _error: (
        render_template("error.html", title="提交内容过大",
                        message="提交内容过大，请减小图片或表单内容后重新提交。"), 413,
    ))
    app.after_request(_disable_private_caching)
    app.add_template_filter(_beijing_time, "beijing_time")
    app.cli.add_command(init_admin_command)
    app.register_blueprint(blueprint)
