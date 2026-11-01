"""Product fields, fixed categories, and the experiment-two lifecycle."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utc_now


class ProductCategory(StrEnum):
    """The five fixed categories agreed during requirements analysis."""

    DIGITAL = "DIGITAL"
    BOOKS = "BOOKS"
    DAILY_NECESSITIES = "DAILY_NECESSITIES"
    CLOTHING = "CLOTHING"
    OTHER = "OTHER"

    @property
    def label(self) -> str:
        """Return the Chinese display label."""
        return CATEGORY_LABELS[self]


CATEGORY_LABELS = dict(zip(ProductCategory, ("数码", "图书", "生活用品", "服饰", "其他")))


class ProductStatus(StrEnum):
    """Reservation-related states are reserved for the next business module."""

    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    PUBLISHED = "PUBLISHED"
    RESERVED = "RESERVED"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    OFF_SHELF = "OFF_SHELF"
    BLOCKED = "BLOCKED"

    @property
    def label(self) -> str:
        """Return the Chinese display label."""
        return STATUS_LABELS[self]


STATUS_LABELS = dict(zip(
    ProductStatus, ("草稿", "待审核", "已发布", "已预约", "已确认", "已完成", "已下架", "已屏蔽"),
))


class Product(Base):
    """A single item owned by a seller; exact prices are persisted as integer cents."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_cents IS NULL OR price_cents > 0", name="ck_product_price"),
        CheckConstraint("version > 0", name="ck_product_version"),
        Index("ix_product_publication", "status", "published_at", "id"),
        Index("ix_product_category_publication", "status", "category", "published_at"),
        Index("ix_product_seller_status", "seller_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    category: Mapped[ProductCategory | None] = mapped_column(Enum(
        ProductCategory, native_enum=False, create_constraint=True, validate_strings=True,
    ))
    price_cents: Mapped[int | None] = mapped_column()
    description: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, native_enum=False, create_constraint=True, validate_strings=True),
        default=ProductStatus.DRAFT, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(default=utc_now, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column()
    latest_reject_reason: Mapped[str | None] = mapped_column(Text)
    latest_block_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1, nullable=False)

    @property
    def price(self) -> Decimal | None:
        """Expose the UML Decimal price without SQLite floating-point rounding."""
        return Decimal(self.price_cents) / 100 if self.price_cents is not None else None
