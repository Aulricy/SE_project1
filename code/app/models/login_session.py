"""Revocable server-side authentication sessions."""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class LoginSession(Base):
    """Only the hash of an opaque browser token is stored in the database."""

    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
