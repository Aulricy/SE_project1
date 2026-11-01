"""Shared SQLAlchemy metadata and UTC timestamps for persisted records."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utc_now() -> datetime:
    """Store UTC without a timezone suffix, matching SQLite DateTime values."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Common declarative base for all business and supporting entities."""
