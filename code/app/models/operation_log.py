"""Business audit records committed with their associated transitions."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now
from app.models.product import ProductStatus
from app.models.reservation import ReservationStatus


class OperationType(StrEnum):
    """Product transitions currently implemented by ProductService."""

    SUBMIT_REVIEW = "SUBMIT_REVIEW"
    WITHDRAW_REVIEW = "WITHDRAW_REVIEW"
    APPROVE_REVIEW = "APPROVE_REVIEW"
    REJECT_REVIEW = "REJECT_REVIEW"
    TAKE_DOWN = "TAKE_DOWN"
    RETURN_TO_DRAFT = "RETURN_TO_DRAFT"
    CREATE_RESERVATION = "CREATE_RESERVATION"
    CONFIRM_RESERVATION = "CONFIRM_RESERVATION"
    CANCEL_RESERVATION = "CANCEL_RESERVATION"
    COMPLETE_TRANSACTION = "COMPLETE_TRANSACTION"
    BLOCK_PRODUCT = "BLOCK_PRODUCT"
    RESTORE_TO_DRAFT = "RESTORE_TO_DRAFT"

    @property
    def label(self) -> str:
        """Human-readable audit event names shared by filters and details."""
        return {
            "SUBMIT_REVIEW": "提交审核", "WITHDRAW_REVIEW": "撤回审核",
            "APPROVE_REVIEW": "审核通过", "REJECT_REVIEW": "审核驳回",
            "TAKE_DOWN": "商品下架", "RETURN_TO_DRAFT": "退回草稿",
            "CREATE_RESERVATION": "创建预约", "CONFIRM_RESERVATION": "确认预约",
            "CANCEL_RESERVATION": "取消预约", "COMPLETE_TRANSACTION": "交易完成",
            "BLOCK_PRODUCT": "屏蔽商品（含预约联动）", "RESTORE_TO_DRAFT": "恢复为草稿",
        }[self.value]


class OperationLog(Base):
    """Committed atomically with a successful product state transition."""

    __tablename__ = "operation_logs"
    __table_args__ = (Index("ix_log_product_time", "product_id", "operated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    operation_type: Mapped[OperationType] = mapped_column(Enum(
        OperationType, native_enum=False, create_constraint=True, validate_strings=True,
    ), nullable=False)
    operated_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    product_status_before: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, native_enum=False), nullable=False,
    )
    product_status_after: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, native_enum=False), nullable=False,
    )
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id"))
    reservation_status_before: Mapped[ReservationStatus | None] = mapped_column(
        Enum(ReservationStatus, native_enum=False),
    )
    reservation_status_after: Mapped[ReservationStatus | None] = mapped_column(
        Enum(ReservationStatus, native_enum=False),
    )
    reason: Mapped[str | None] = mapped_column(Text)
