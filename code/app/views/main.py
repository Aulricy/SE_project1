"""Initial landing page and database readiness check."""

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_session
from app.views.products import catalog

blueprint = Blueprint("main", __name__)


@blueprint.get("/")
def index():
    """Use the real public catalogue as the home page."""
    return catalog()


@blueprint.get("/health")
def health():
    """Check both application responsiveness and an actual database connection."""
    try:
        get_session().execute(text("SELECT 1"))
    except SQLAlchemyError:
        current_app.logger.exception("Database readiness check failed")
        return jsonify(status="error", database="unavailable"), 503
    return jsonify(status="ok", database="ok")
