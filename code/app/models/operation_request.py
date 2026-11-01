"""Durable successful-request deduplication, scoped to the authenticated actor."""

from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now
from app.models.operation_log import OperationType


class OperationRequestRecord(Base):
    """Persist only with a successful transaction and reference its original reservation."""

    __tablename__ = "operation_requests"
    __table_args__ = (
        UniqueConstraint("operator_id", "request_key", name="uq_actor_request_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[OperationType] = mapped_column(
        Enum(OperationType, native_enum=False), nullable=False,
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
