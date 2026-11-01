"""User identity and the two persistent account roles."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class UserRole(StrEnum):
    """Buyer and seller are capabilities of MEMBER, rather than separate accounts."""

    MEMBER = "MEMBER"
    ADMIN = "ADMIN"


class User(Base):
    """A uniquely named account with a salted password hash."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(username) BETWEEN 1 AND 40", name="ck_username_length"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, create_constraint=True, validate_strings=True),
        nullable=False,
        default=UserRole.MEMBER,
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
