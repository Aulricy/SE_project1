"""Interactive local provisioning commands."""

import click
from flask.cli import with_appcontext

from app.services.users import AccountError, UserService


@click.command("init-admin")
@click.option("--username", default="admin", show_default=True, help="首个管理员用户名。")
@with_appcontext
def init_admin_command(username: str) -> None:
    """Provision the initial administrator without a default or command-line password."""
    password = click.prompt("设置首个管理员密码（8–128 个字符）", hide_input=True, confirmation_prompt=True)
    try:
        account = UserService.bootstrap_admin(username, password)
    except AccountError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"首个管理员 {account.username}（编号 {account.id}）已预置。密码不会保存为明文。")
