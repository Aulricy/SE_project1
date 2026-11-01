"""Reservations, historical prices, and database-enforced active uniqueness."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class ReservationStatus(StrEnum):
    """Pending and confirmed reservations both occupy their product."""

    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

    @property
    def label(self) -> str:
        """Return the experiment-two display name."""
        return STATUS_LABELS[self]


STATUS_LABELS = {
    ReservationStatus.PENDING_CONFIRMATION: "待卖家确认",
    ReservationStatus.CONFIRMED: "已确认",
    ReservationStatus.CANCELLED: "已取消",
    ReservationStatus.COMPLETED: "已完成",
}
ACTIVE_STATUSES = (ReservationStatus.PENDING_CONFIRMATION, ReservationStatus.CONFIRMED)


class Reservation(Base):
    """An immutable buyer/product link with a price captured at booking time."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("reserved_price_cents > 0", name="ck_reservation_price"),
        CheckConstraint("version > 0", name="ck_reservation_version"),
        CheckConstraint(
            "status NOT IN ('CONFIRMED', 'COMPLETED') OR confirmed_at IS NOT NULL",
            name="ck_reservation_confirmation_time",
        ),
        CheckConstraint(
            "(status = 'COMPLETED' AND completed_at IS NOT NULL) OR "
            "(status != 'COMPLETED' AND completed_at IS NULL)",
            name="ck_reservation_completion_time",
        ),
        CheckConstraint(
            "(status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL "
            "AND cancel_reason IS NOT NULL AND length(trim(cancel_reason)) > 0) OR "
            "(status != 'CANCELLED' AND cancelled_at IS NULL AND cancelled_by IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_reservation_cancellation",
        ),
        Index(
            "uq_reservation_active_product", "product_id", unique=True,
            sqlite_where=text("status IN ('PENDING_CONFIRMATION', 'CONFIRMED')"),
        ),
        Index("ix_reservation_buyer_time", "buyer_id", "created_at", "id"),
        Index("ix_reservation_product_time", "product_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    reserved_price_cents: Mapped[int] = mapped_column(nullable=False)
    reserved_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reserved_description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False, create_constraint=True, validate_strings=True),
        default=ReservationStatus.PENDING_CONFIRMATION, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column()
    cancelled_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1, nullable=False)

    @property
    def reserved_price(self) -> Decimal:
        """Return the original exact price, independent of later product edits."""
        return Decimal(self.reserved_price_cents) / 100
