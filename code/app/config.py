"""Local configuration shared by the application factory."""

from datetime import timedelta
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL


def load_config(project_dir: Path, instance_dir: Path) -> dict:
    """Read local defaults; explicit environment variables take precedence."""
    load_dotenv(project_dir / ".env", override=False, encoding="utf-8-sig")
    default_url = URL.create("sqlite", database=str(instance_dir / "secondhand.sqlite3"))
    return {
        "SECRET_KEY": os.environ.get("SECRET_KEY"),
        "DATABASE_URL": os.environ.get("DATABASE_URL") or default_url,
        "INITIAL_ADMIN_ID": 1,
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=8),
        "SESSION_COOKIE_NAME": "secondhand_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_REFRESH_EACH_REQUEST": False,
        "MAX_CONTENT_LENGTH": 5 * 1024 * 1024,
        "MAX_IMAGE_BYTES": 3 * 1024 * 1024,
        "UPLOAD_FOLDER": str(instance_dir / "uploads"),
    }
