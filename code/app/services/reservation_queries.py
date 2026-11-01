"""Participant-only reservation history and joined detail reads."""

from dataclasses import dataclass
import math

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased
from werkzeug.exceptions import NotFound

from app.database import get_engine
from app.models import Product, ProductStatus, Reservation, ReservationStatus, User
from app.services.reservation_input import ReservationQuery
from app.services.users import UserService


@dataclass
class ReservationView:
    """Related product information and display names, without password hash columns."""

    reservation: Reservation
    product: Product
    buyer_name: str
    seller_name: str
    cancelled_by_name: str | None

    def may_view_image(self, actor_id: int) -> bool:
        """Former buyers must not see a seller's newly edited private draft image."""
        if actor_id == self.product.seller_id or self.product.status == ProductStatus.PUBLISHED:
            return True
        return (self.reservation.status, self.product.status) in {
            (ReservationStatus.PENDING_CONFIRMATION, ProductStatus.RESERVED),
            (ReservationStatus.CONFIRMED, ProductStatus.CONFIRMED),
            (ReservationStatus.COMPLETED, ProductStatus.COMPLETED),
        }


@dataclass
class ReservationPage:
    """Server-side history pagination."""

    items: list[ReservationView]
    total: int
    page: int
    per_page: int = 12

    @property
    def pages(self) -> int:
        """Include an empty first page for new accounts."""
        return max(1, math.ceil(self.total / self.per_page))


def _joined_query():
    """Fetch all display fields in one SQL query."""
    buyer = aliased(User, name="reservation_buyer")
    seller = aliased(User, name="reservation_seller")
    canceller = aliased(User, name="reservation_canceller")
    return (
        select(Reservation, Product, buyer.username, seller.username, canceller.username)
        .join(Product, Reservation.product_id == Product.id)
        .join(buyer, Reservation.buyer_id == buyer.id)
        .join(seller, Product.seller_id == seller.id)
        .outerjoin(canceller, Reservation.cancelled_by == canceller.id)
    )


class ReservationQueries:
    """Read only records in which the authenticated account participates."""

    @staticmethod
    def get_detail(reservation_id: int) -> ReservationView:
        """Unknown and unrelated reservations both appear unavailable."""
        actor = UserService.require_member()
        with Session(get_engine()) as database:
            row = database.execute(_joined_query().where(
                Reservation.id == reservation_id,
                or_(Reservation.buyer_id == actor.id, Product.seller_id == actor.id),
            )).first()
            if row is None:
                raise NotFound()
            return ReservationView(*row)

    @staticmethod
    def search(query: ReservationQuery) -> ReservationPage:
        """Buying and selling are ownership scopes, never caller-selected account IDs."""
        actor = UserService.require_member()
        conditions = [
            Reservation.buyer_id == actor.id if query.side == "buying"
            else Product.seller_id == actor.id,
        ]
        if query.status is not None:
            conditions.append(Reservation.status == query.status)
        if query.product_id is not None:
            conditions.append(Reservation.product_id == query.product_id)
        with Session(get_engine()) as database:
            total = database.scalar(
                select(func.count()).select_from(Reservation)
                .join(Product, Reservation.product_id == Product.id).where(*conditions),
            )
            rows = database.execute(
                _joined_query().where(*conditions)
                .order_by(Reservation.created_at.desc(), Reservation.id.desc())
                .limit(query.per_page).offset((query.page - 1) * query.per_page),
            ).all()
            return ReservationPage([ReservationView(*row) for row in rows], total, query.page)
