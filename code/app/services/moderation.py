"""Administrator product moderation, with atomic reservation cancellation."""

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from werkzeug.exceptions import NotFound

from app.database import get_engine
from app.models import OperationLog, OperationType, Product, ProductStatus
from app.models.reservation import ReservationStatus
from app.services.product_input import ProductError
from app.services.reservation_input import ReservationError
from app.services.reservations import ReservationService
from app.services.users import UserService


def moderate_product(product_id: int, version: int, operation: str, reason: str) -> Product:
    """Claim a version before reads; product, optional reservation and log commit together."""
    actor = UserService.require_admin()
    if operation not in {"block", "restore"}:
        raise ProductError("不支持的管理员操作。", 404)
    reason = reason.strip()
    if operation == "block" and not reason:
        raise ProductError("屏蔽商品必须填写原因。", field="reason")
    if len(reason) > 1000:
        raise ProductError("原因最多 1000 个字符。", field="reason")
    if not isinstance(version, int) or not 1 <= version <= 9223372036854775807:
        raise ProductError("商品版本无效，请刷新后重试。")
    states = ((ProductStatus.BLOCKED,) if operation == "restore" else tuple(
        state for state in ProductStatus if state not in {
            ProductStatus.BLOCKED, ProductStatus.COMPLETED,
        }
    ))
    try:
        with Session(get_engine(), expire_on_commit=False) as database, database.begin():
            product = database.scalar(update(Product).where(
                Product.id == product_id, Product.version == version, Product.status.in_(states),
            ).values(version=Product.version + 1).returning(Product))
            if product is None:
                if database.get(Product, product_id) is None:
                    raise NotFound()
                raise ProductError("商品状态或信息已变化，本次未生效，请刷新后再操作。", 409)
            before = product.status
            # This helper checks the matching active state even on restore, so corruption
            # cannot silently resurrect or abandon an active reservation.
            if operation == "block":
                reservation = ReservationService.cancel_for_blocking(
                    database, product, actor.id, reason,
                )
                product.latest_block_reason = reason
                product.status = ProductStatus.BLOCKED
            else:
                reservation = ReservationService.cancel_for_blocking(
                    database, product, actor.id, reason,
                )
                product.status = ProductStatus.DRAFT
            database.add(OperationLog(
                operator_id=actor.id, product_id=product.id,
                operation_type=(OperationType.BLOCK_PRODUCT if operation == "block"
                                else OperationType.RESTORE_TO_DRAFT),
                product_status_before=before, product_status_after=product.status,
                reservation_id=reservation.id if reservation else None,
                reservation_status_before=(ReservationStatus.PENDING_CONFIRMATION
                    if before == ProductStatus.RESERVED else ReservationStatus.CONFIRMED)
                    if reservation else None,
                reservation_status_after=ReservationStatus.CANCELLED if reservation else None,
                reason=reason if operation == "block" else None,
            ))
        return product
    except ReservationError as error:
        raise ProductError(str(error), error.status_code) from error
    except IntegrityError as error:
        raise ProductError("关联数据发生冲突，本次操作已回滚，请刷新后再试。", 409) from error
    except OperationalError as error:
        if getattr(error.orig, "sqlite_errorcode", 0) & 255 in {5, 6}:
            raise ProductError("当前有其他操作正在处理，请稍后刷新重试。", 409) from error
        raise
