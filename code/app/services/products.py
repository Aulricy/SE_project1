"""ProductService: owner edits, review transitions, version checks and scoped queries."""

from dataclasses import dataclass
from functools import wraps
import math

from flask import g
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import Forbidden, NotFound

from app.database import get_engine
from app.identifiers import positive_integer
from app.models import User, UserRole
from app.models.base import utc_now
from app.models.operation_log import OperationLog, OperationType
from app.models.product import Product, ProductCategory, ProductStatus
from app.models.reservation import ACTIVE_STATUSES, Reservation
from app.services.product_images import discard_image, store_image
from app.services.product_input import ProductError, ProductInput, ProductQuery
from app.services.users import UserService
from app.services.moderation import moderate_product



def _product_write(operation):
    """Report expected database conflicts after rollback and failed-upload cleanup finish."""
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except IntegrityError as error:
            raise ProductError("商品关联数据发生冲突，本次未保存，请刷新后再试。", 409) from error
        except OperationalError as error:
            if getattr(error.orig, "sqlite_errorcode", 0) & 255 in {5, 6}:
                raise ProductError("当前有其他操作正在处理，本次未保存，请刷新后重试。", 409) from error
            raise
    return guarded


def _claim_product(database: Session, product_id: int, version: int,
                   states: tuple, owner: User | None) -> Product:
    """The first SQL is a conditional write, avoiding SQLite read-to-write lock upgrades."""
    try:
        version = positive_integer(version)
    except ValueError as error:
        raise ProductError("商品版本无效，请刷新后重试。") from error
    condition = [
        Product.id == product_id, Product.version == version, Product.status.in_(states),
        ~select(Reservation.id).where(
            Reservation.product_id == Product.id, Reservation.status.in_(ACTIVE_STATUSES),
        ).exists(),
    ]
    if owner is not None:
        condition.append(Product.seller_id == owner.id)
    product = database.scalar(
        update(Product).where(*condition).values(version=Product.version + 1).returning(Product),
    )
    if product is None:
        current = database.get(Product, product_id)
        if current is None:
            raise NotFound()
        if owner is not None and current.seller_id != owner.id:
            raise Forbidden()
        raise ProductError("商品状态或信息已变化，本次操作未生效，请刷新后重试。", 409)
    return product


@dataclass
class ProductView:
    """A product and its public seller name, without joining the user's password hash."""

    product: Product
    seller_name: str


@dataclass
class ProductPage:
    """Server-side pagination and metadata for list pages."""

    items: list[ProductView]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        """Always offer a first page, including for empty results."""
        return max(1, math.ceil(self.total / self.per_page))


class ProductService:
    """Implement the product operations from the experiment-two class diagram."""

    @staticmethod
    @_product_write
    def create_draft(payload: ProductInput, *, submit: bool = False) -> Product:
        """Set seller, identifier, status, and version on the server."""
        actor = UserService.require_member()
        values = payload.validated(complete=submit)
        filename = store_image(payload.image)
        try:
            with Session(get_engine(), expire_on_commit=False) as database, database.begin():
                product = Product(
                    seller_id=actor.id, image_path=filename, **values,
                    status=ProductStatus.PENDING_REVIEW if submit else ProductStatus.DRAFT,
                )
                database.add(product)
                database.flush()
                if submit:
                    database.add(OperationLog(
                        operator_id=actor.id, product_id=product.id,
                        operation_type=OperationType.SUBMIT_REVIEW,
                        product_status_before=ProductStatus.DRAFT,
                        product_status_after=ProductStatus.PENDING_REVIEW,
                    ))
            return product
        except Exception:
            discard_image(filename)
            raise

    @staticmethod
    @_product_write
    def update_draft(product_id: int, version: int, payload: ProductInput,
                     *, submit: bool = False) -> Product:
        """Save only the owner's current draft, atomically incrementing its version."""
        actor = UserService.require_member()
        values = payload.validated(complete=submit)
        filename = store_image(payload.image)
        old_filename = None
        try:
            with Session(get_engine(), expire_on_commit=False) as database, database.begin():
                product = _claim_product(
                    database, product_id, version, (ProductStatus.DRAFT,), actor,
                )
                for field, value in values.items():
                    setattr(product, field, value)
                if filename is not None or payload.remove_image:
                    old_filename = product.image_path
                    product.image_path = filename
                if submit:
                    product.status = ProductStatus.PENDING_REVIEW
                    database.add(OperationLog(
                        operator_id=actor.id, product_id=product.id,
                        operation_type=OperationType.SUBMIT_REVIEW,
                        product_status_before=ProductStatus.DRAFT,
                        product_status_after=ProductStatus.PENDING_REVIEW,
                    ))
        except Exception:
            discard_image(filename)
            raise
        discard_image(old_filename)
        return product

    @staticmethod
    @_product_write
    def transition(product_id: int, version: int, operation: str, reason: str = "") -> Product:
        """Update a permitted state and its operation log in the same transaction."""
        if operation in {"block", "restore"}:
            return moderate_product(product_id, version, operation, reason)
        try:
            states, target, operation_type, administrator = TRANSITIONS[operation]
        except KeyError as error:
            raise ProductError("不支持的商品操作。", 404) from error
        actor = UserService.require_admin() if administrator else UserService.require_member()
        reason = reason.strip()
        if operation == "reject" and not reason:
            raise ProductError("请填写驳回原因。", field="reason")
        if len(reason) > 1000:
            raise ProductError("原因最多 1000 个字符。", field="reason")
        with Session(get_engine(), expire_on_commit=False) as database, database.begin():
            product = _claim_product(
                database, product_id, version, states, None if administrator else actor,
            )
            previous = product.status
            if operation in {"submit", "approve"}:
                ProductInput(
                    product.name or "", product.category.value if product.category else "",
                    str(product.price) if product.price is not None else "",
                    product.description or "",
                ).validated(complete=True)
            if operation == "approve":
                product.published_at = utc_now()
            elif operation == "reject":
                product.latest_reject_reason = reason
            product.status = target
            database.add(OperationLog(
                operator_id=actor.id, product_id=product.id, operation_type=operation_type,
                product_status_before=previous, product_status_after=target,
                reason=reason if operation == "reject" else None,
            ))
        return product

    @staticmethod
    def get_detail(product_id: int) -> ProductView:
        """Unpublished product information is only visible to its seller or an admin."""
        with Session(get_engine()) as database:
            row = database.execute(
                select(Product, User.username).join(User, Product.seller_id == User.id)
                .where(Product.id == product_id)
            ).first()
            if row is None:
                raise NotFound()
            product, seller_name = row
            actor = g.get("user")
            private_access = actor and (
                actor.role == UserRole.ADMIN or actor.id == product.seller_id
            )
            if product.status != ProductStatus.PUBLISHED and not private_access:
                raise NotFound()
            return ProductView(product, seller_name)

    @staticmethod
    def search(query: ProductQuery, scope: str = "public") -> ProductPage:
        """Apply authorization, combination filters, deterministic sorting and paging in SQL."""
        conditions = []
        if scope == "own":
            conditions.append(Product.seller_id == UserService.require_member().id)
        elif scope == "admin":
            UserService.require_admin()
        elif scope == "public":
            conditions.append(Product.status == ProductStatus.PUBLISHED)
        else:
            raise ProductError("查询范围无效。")
        if query.status is not None:
            conditions.append(Product.status == query.status)
        if query.keyword:
            conditions.append(Product.name.contains(query.keyword, autoescape=True))
        if query.category is not None:
            conditions.append(Product.category == query.category)
        if query.min_price is not None:
            conditions.append(Product.price_cents >= query.min_price)
        if query.max_price is not None:
            conditions.append(Product.price_cents <= query.max_price)
        ordering = (
            (Product.published_at.desc(), Product.id.desc()) if scope == "public"
            else (Product.created_at.desc(), Product.id.desc())
        )
        with Session(get_engine()) as database:
            total = database.scalar(select(func.count()).select_from(Product).where(*conditions))
            rows = database.execute(
                select(Product, User.username).join(User, Product.seller_id == User.id)
                .where(*conditions).order_by(*ordering).limit(query.per_page)
                .offset((query.page - 1) * query.per_page)
            ).all()
            return ProductPage(
                [ProductView(*row) for row in rows], total, query.page, query.per_page,
            )


TRANSITIONS = {
    "submit": ((ProductStatus.DRAFT,), ProductStatus.PENDING_REVIEW,
               OperationType.SUBMIT_REVIEW, False),
    "withdraw": ((ProductStatus.PENDING_REVIEW,), ProductStatus.DRAFT,
                 OperationType.WITHDRAW_REVIEW, False),
    "approve": ((ProductStatus.PENDING_REVIEW,), ProductStatus.PUBLISHED,
                OperationType.APPROVE_REVIEW, True),
    "reject": ((ProductStatus.PENDING_REVIEW,), ProductStatus.DRAFT,
               OperationType.REJECT_REVIEW, True),
    "take-down": ((ProductStatus.PUBLISHED,), ProductStatus.OFF_SHELF,
                  OperationType.TAKE_DOWN, False),
    "return-draft": ((ProductStatus.PUBLISHED, ProductStatus.OFF_SHELF),
                     ProductStatus.DRAFT, OperationType.RETURN_TO_DRAFT, False),
}

CATEGORY_OPTIONS = [(value.value, value.label) for value in ProductCategory]
STATUS_OPTIONS = [(value.value, value.label) for value in ProductStatus]
