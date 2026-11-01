"""Application factory for the secondhand trading platform."""

from pathlib import Path

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from app import database
from app.config import load_config
from app.identifiers import ResourceIDConverter
from app.views import admin, auth, products, reservations
from app.views.main import blueprint

csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    """Build an independently configured application instance."""
    project_dir = Path(__file__).resolve().parent.parent
    instance_dir = project_dir / "instance"
    app = Flask(__name__, instance_path=str(instance_dir), instance_relative_config=True)
    app.config.from_mapping(load_config(project_dir, instance_dir))
    if test_config is not None:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("请在 code/.env 中设置 SECRET_KEY，配置方法见 code/README.md。")
    initial_id = app.config["INITIAL_ADMIN_ID"]
    if not isinstance(initial_id, int) or initial_id < 1:
        raise RuntimeError("INITIAL_ADMIN_ID 须为正整数。")

    instance_dir.mkdir(parents=True, exist_ok=True)
    app.url_map.converters["int"] = ResourceIDConverter
    database.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(blueprint)
    auth.init_app(app)
    products.init_app(app)
    reservations.init_app(app)
    admin.init_app(app)
    return app
