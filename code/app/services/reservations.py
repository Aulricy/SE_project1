"""Atomic booking and trade transitions with durable request replay semantics."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden, NotFound

from app.database import get_engine
from app.models import OperationLog, OperationRequestRecord, OperationType, Product, ProductStatus
from app.models.base import utc_now
from app.models.reservation import ACTIVE_STATUSES, Reservation, ReservationStatus
from app.services.reservation_input import ReservationCommand, ReservationError
from app.services.users import UserService

_OPERATION_TYPES = {
    "reserve": OperationType.CREATE_RESERVATION,
    "confirm": OperationType.CONFIRM_RESERVATION,
    "cancel": OperationType.CANCEL_RESERVATION,
    "complete": OperationType.COMPLETE_TRANSACTION,
}
_PRODUCT_STATES = {
    ReservationStatus.PENDING_CONFIRMATION: ProductStatus.RESERVED,
    ReservationStatus.CONFIRMED: ProductStatus.CONFIRMED,
}


@dataclass
class ReservationResult:
    """Replay returns the original reservation's current state, never a new booking."""

    reservation: Reservation
    replayed: bool = False


def _replay(database: Session, actor_id: int, command: ReservationCommand):
    """Check durable successful requests before inspecting current business states."""
    previous = database.scalar(select(OperationRequestRecord).where(
        OperationRequestRecord.operator_id == actor_id,
        OperationRequestRecord.request_key == command.request_key,
    ))
    if previous is None:
        return None
    if previous.request_fingerprint != command.fingerprint:
        raise ReservationError("请求标识已用于不同内容，请刷新页面后发起新操作。", 409)
    reservation = database.get(Reservation, previous.reservation_id)
    if reservation is None:
        raise ReservationError("原预约记录不可用，本次未执行新操作。", 409)
    return ReservationResult(reservation, replayed=True)


def _new_reservation(database: Session, actor, command: ReservationCommand):
    """Create one pending reservation and occupy the still-published product."""
    product = database.get(Product, command.target_id)
    if product is None:
        raise NotFound()
    if product.seller_id == actor.id:
        raise ReservationError("不能预约本人发布的商品。")
    if product.status != ProductStatus.PUBLISHED or product.version != command.expected_version:
        raise ReservationError("商品状态或信息已变化，请刷新后重新查看。", 409)
    occupied = database.scalar(select(Reservation.id).where(
        Reservation.product_id == product.id, Reservation.status.in_(ACTIVE_STATUSES),
    ))
    if (occupied is not None or product.price_cents is None or product.price_cents <= 0
            or not product.name or not product.description):
        raise ReservationError("商品当前不可预约，本次操作未生效。", 409)
    reservation = Reservation(
        product_id=product.id, buyer_id=actor.id, reserved_price_cents=product.price_cents,
        reserved_name=product.name, reserved_description=product.description,
        status=ReservationStatus.PENDING_CONFIRMATION,
    )
    product.status = ProductStatus.RESERVED
    product.version += 1
    database.add(reservation)
    database.flush()
    return reservation, product, ProductStatus.PUBLISHED, None


def _advance_reservation(database: Session, actor, command: ReservationCommand):
    """Apply role, exact page version, and matching product/reservation state checks."""
    reservation = database.get(Reservation, command.target_id)
    if reservation is None:
        raise NotFound()
    product = database.get(Product, reservation.product_id)
    if product is None:
        raise ReservationError("关联商品不可用。", 409)
    if command.operation == "confirm" and actor.id != product.seller_id:
        raise Forbidden()
    if command.operation == "complete" and actor.id != reservation.buyer_id:
        raise Forbidden()
    if actor.id not in {product.seller_id, reservation.buyer_id}:
        raise Forbidden()
    previous = reservation.status
    expected = (
        ReservationStatus.PENDING_CONFIRMATION if command.operation == "confirm"
        else ReservationStatus.CONFIRMED
    )
    valid_state = (
        previous in ACTIVE_STATUSES if command.operation == "cancel" else previous == expected
    )
    if reservation.version != command.expected_version or not valid_state:
        raise ReservationError("预约状态或信息已变化，本次未生效，请刷新后再操作。", 409)
    if product.status != _PRODUCT_STATES.get(previous):
        raise ReservationError("商品与预约当前状态不一致，本次未生效。", 409)
    if command.operation == "cancel" and not command.reason.strip():
        raise ReservationError("取消或拒绝预约时必须填写原因。")
    product_before = product.status
    timestamp = utc_now()
    if command.operation == "confirm":
        reservation.status = ReservationStatus.CONFIRMED
        reservation.confirmed_at = timestamp
        product.status = ProductStatus.CONFIRMED
    elif command.operation == "cancel":
        reservation.status = ReservationStatus.CANCELLED
        reservation.cancelled_at = timestamp
        reservation.cancelled_by = actor.id
        reservation.cancel_reason = command.reason.strip()
        product.status = ProductStatus.PUBLISHED
    else:
        reservation.status = ReservationStatus.COMPLETED
        reservation.completed_at = timestamp
        product.status = ProductStatus.COMPLETED
    reservation.version += 1
    product.version += 1
    return reservation, product, product_before, previous


def _execute(command: ReservationCommand) -> ReservationResult:
    """Serialize SQLite writes before any read; commit four kinds of records together."""
    actor = UserService.require_member()
    command.validate()
    try:
        with Session(get_engine(), expire_on_commit=False) as database, database.begin():
            # A no-op first write obtains SQLite's writer lock without changing any version.
            # Unlike an early SELECT, it cannot establish a stale read-to-write snapshot.
            table = Product if command.operation == "reserve" else Reservation
            database.execute(update(table).where(table.id == command.target_id).values(
                version=table.version,
            ))
            replay = _replay(database, actor.id, command)
            if replay is not None:
                return replay
            handler = _new_reservation if command.operation == "reserve" else _advance_reservation
            reservation, product, product_before, reservation_before = handler(
                database, actor, command,
            )
            database.add(OperationRequestRecord(
                operator_id=actor.id, request_key=command.request_key,
                operation_type=_OPERATION_TYPES[command.operation],
                request_fingerprint=command.fingerprint, reservation_id=reservation.id,
            ))
            database.add(OperationLog(
                operator_id=actor.id, product_id=product.id, reservation_id=reservation.id,
                operation_type=_OPERATION_TYPES[command.operation],
                product_status_before=product_before, product_status_after=product.status,
                reservation_status_before=reservation_before,
                reservation_status_after=reservation.status,
                reason=reservation.cancel_reason if command.operation == "cancel" else None,
            ))
        return ReservationResult(reservation)
    except IntegrityError as error:
        raise ReservationError("预约状态或请求发生冲突，本次数据未保存，请刷新后再试。", 409) from error
    except OperationalError as error:
        code = getattr(error.orig, "sqlite_errorcode", 0)
        if code & 255 in {5, 6}:
            raise ReservationError("当前有其他操作正在处理，请稍后刷新重试。", 409) from error
        raise


class ReservationService:
    """Implement reserve, confirm, cancel and complete from the experiment-two service."""

    @staticmethod
    def reserve(product_id: int, expected_version: int, request_key: str) -> ReservationResult:
        """Reserve someone else's published product at its current exact price."""
        return _execute(ReservationCommand("reserve", product_id, expected_version, request_key))

    @staticmethod
    def confirm(reservation_id: int, expected_version: int, request_key: str) -> ReservationResult:
        """Only the product's seller may confirm a pending reservation."""
        return _execute(ReservationCommand(
            "confirm", reservation_id, expected_version, request_key,
        ))

    @staticmethod
    def cancel(reservation_id: int, expected_version: int, request_key: str,
               reason: str) -> ReservationResult:
        """Either participant may cancel a matching pending/confirmed page with a reason."""
        return _execute(ReservationCommand(
            "cancel", reservation_id, expected_version, request_key, reason,
        ))

    @staticmethod
    def complete(reservation_id: int, expected_version: int, request_key: str) -> ReservationResult:
        """Only the original buyer may complete the confirmed trade."""
        return _execute(ReservationCommand(
            "complete", reservation_id, expected_version, request_key,
        ))


    @staticmethod
    def cancel_for_blocking(database: Session, product: Product, operator_id: int,
                            reason: str) -> Reservation | None:
        """Internal collaboration: caller owns authorization, writer lock and transaction."""
        reservation = database.scalar(select(Reservation).where(
            Reservation.product_id == product.id, Reservation.status.in_(ACTIVE_STATUSES),
        ))
        expected = _PRODUCT_STATES.get(reservation.status) if reservation else None
        if ((reservation is not None and product.status != expected)
                or (reservation is None and product.status in _PRODUCT_STATES.values())):
            raise ReservationError("商品与有效预约状态不一致，屏蔽未生效。", 409)
        if reservation is not None:
            reservation.status = ReservationStatus.CANCELLED
            reservation.cancelled_at = utc_now()
            reservation.cancelled_by = operator_id
            reservation.cancel_reason = reason
            reservation.version += 1
        return reservation
