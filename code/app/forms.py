"""Server-validated account forms with CSRF protection."""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField
from wtforms.validators import DataRequired, EqualTo, InputRequired, Length


def _strip_username(value: str | None) -> str:
    """Trim names without ever trimming passwords."""
    return (value or "").strip()


class LoginForm(FlaskForm):
    """Shared login entry for ordinary users and administrators."""

    username = StringField(
        "用户名", filters=[_strip_username],
        validators=[InputRequired("请填写用户名。"), Length(min=1, max=40, message="用户名须为 1–40 个字符。")],
    )
    password = PasswordField(
        "密码", validators=[InputRequired("请填写密码。"), Length(max=128, message="密码最多 128 个字符。")],
    )


class RegistrationForm(LoginForm):
    """Public registration exposes no role or identifier field."""

    password = PasswordField(
        "密码",
        validators=[InputRequired("请填写密码。"), Length(min=8, max=128, message="密码须为 8–128 个字符。")],
    )
    confirm = PasswordField(
        "确认密码",
        validators=[InputRequired("请再次输入密码。"), EqualTo("password", message="两次输入的密码不一致。")],
    )


class AdminAccountForm(RegistrationForm):
    """Approval and account creation are a single initial-administrator action."""

    approved = BooleanField(
        "我已核对申请，批准创建此管理员账号。",
        validators=[DataRequired("请先确认已批准本次管理员申请。")],
    )
