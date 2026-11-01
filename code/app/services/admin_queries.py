"""Read-only administrator statistics and audit queries, in Beijing time."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
import math
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from werkzeug.exceptions import NotFound

from app.database import get_engine
from app.models import OperationLog, OperationType, Reservation, User
from app.models.reservation import ReservationStatus
from app.services.users import UserService

BEIJING = timezone(timedelta(hours=8))


class AdminQueryError(Exception):
    """Invalid query input; never silently substitute a different range."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=BEIJING).astimezone(UTC).replace(tzinfo=None)


def _identifier(value: str) -> int | None:
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,19}", value) or not 1 <= int(value) <= 9223372036854775807:
        raise AdminQueryError("对象编号须为有效的正整数。")
    return int(value)


def _page(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", value) or not 1 <= int(value) <= 100000:
        raise AdminQueryError("页码须为 1–100000。")
    return int(value)


def _timestamp(value: str, *, end: bool = False) -> datetime | None:
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?", value):
        raise AdminQueryError("请输入有效的北京时间，精确到秒。")
    try:
        result = _utc(datetime.fromisoformat(value))
        return result + timedelta(seconds=1) if end else result
    except (ValueError, OverflowError) as error:
        raise AdminQueryError("时间超出有效范围。") from error


@dataclass(frozen=True)
class StatisticsQuery:
    """Inclusive local dates represented by a half-open UTC interval."""

    start_date: date
    end_date: date
    start: datetime
    end: datetime
    page: int = 1

    @classmethod
    def from_mapping(cls, values):
        """Reject missing, noncanonical, reversed or unrepresentable date ranges."""
        try:
            start = date.fromisoformat(values.get("start", ""))
            end = date.fromisoformat(values.get("end", ""))
            if start.isoformat() != values.get("start") or end.isoformat() != values.get("end"):
                raise ValueError()
            if start > end:
                raise AdminQueryError("起始日期不能晚于结束日期。")
            lower = _utc(datetime.combine(start, time.min))
            upper = _utc(datetime.combine(end + timedelta(days=1), time.min))
        except (ValueError, OverflowError) as error:
            raise AdminQueryError("请选择有效的起止日期，日期超出范围或格式无效。") from error
        return cls(start, end, lower, upper, _page(values.get("page", "1")))


@dataclass(frozen=True)
class LogQuery:
    """Optional combined filters, never accepting arbitrary SQL field names."""

    start: datetime | None = None
    end: datetime | None = None
    operator: str = ""
    product_id: int | None = None
    reservation_id: int | None = None
    operation_type: OperationType | None = None
    page: int = 1

    @classmethod
    def from_mapping(cls, values):
        """Include both endpoint seconds; operator matches exact username or identifier."""
        start = _timestamp(values.get("start", ""))
        end = _timestamp(values.get("end", ""), end=True)
        if start and end and start >= end:
            raise AdminQueryError("开始时间不能晚于结束时间。")
        operator = values.get("operator", "").strip()
        if len(operator) > 100:
            raise AdminQueryError("操作人输入最多 100 个字符。")
        try:
            kind = OperationType(values["type"]) if values.get("type") else None
        except ValueError as error:
            raise AdminQueryError("操作类型无效。") from error
        return cls(start, end, operator, _identifier(values.get("product_id", "")),
                   _identifier(values.get("reservation_id", "")), kind,
                   _page(values.get("page", "1")))


@dataclass
class QueryPage:
    """One bounded page and metadata, ordered by time then identifier."""

    items: list
    total: int
    page: int
    per_page: int = 20

    @property
    def pages(self) -> int:
        """Even an empty result has a first page."""
        return max(1, math.ceil(self.total / self.per_page))


@dataclass
class StatisticsResult:
    """Computed from reservation snapshots; never stored as a separate total."""

    completed_count: int
    total_cents: int
    records: QueryPage

    @property
    def formatted_amount(self) -> str:
        """Format exact Decimal cents without converting a total to binary floating point."""
        return f"{self.total_amount:.2f}"

    @property
    def total_amount(self) -> Decimal:
        """Convert cents only after aggregation, without floating-point accumulation."""
        return Decimal(self.total_cents) / 100


@dataclass
class LogView:
    """Expose audit content and actor name without selecting credentials or sessions."""

    log: OperationLog
    operator_name: str


class StatisticsService:
    """REQ-014: first completion date and reserved price, administrators only."""

    @staticmethod
    def get_completed_statistics(query: StatisticsQuery) -> StatisticsResult:
        """Read aggregate and displayed rows from the same database snapshot."""
        UserService.require_admin()
        conditions = (
            Reservation.status == ReservationStatus.COMPLETED,
            Reservation.completed_at >= query.start, Reservation.completed_at < query.end,
        )
        with Session(get_engine()) as database:
            count, cents = database.execute(select(
                func.count(Reservation.id),
                func.coalesce(func.sum(Reservation.reserved_price_cents), 0),
            ).where(*conditions)).one()
            rows = list(database.scalars(select(Reservation).where(*conditions)
                .order_by(Reservation.completed_at.desc(), Reservation.id.desc())
                .limit(20).offset((query.page - 1) * 20)))
            return StatisticsResult(count, cents, QueryPage(rows, count, query.page))


class LogService:
    """REQ-015: append through business transactions; expose only read queries."""

    @staticmethod
    def query(query: LogQuery) -> QueryPage:
        """All supplied filters must match, with SQL pagination and stable ordering."""
        UserService.require_admin()
        conditions = []
        if query.start:
            conditions.append(OperationLog.operated_at >= query.start)
        if query.end:
            conditions.append(OperationLog.operated_at < query.end)
        if query.operator:
            actor_match = User.username == query.operator
            if re.fullmatch(r"[0-9]{1,19}", query.operator):
                identifier = int(query.operator)
                if identifier <= 9223372036854775807:
                    actor_match = or_(actor_match, User.id == identifier)
            conditions.append(actor_match)
        for field, value in ((OperationLog.product_id, query.product_id),
                             (OperationLog.reservation_id, query.reservation_id),
                             (OperationLog.operation_type, query.operation_type)):
            if value is not None:
                conditions.append(field == value)
        with Session(get_engine()) as database:
            base = select(OperationLog, User.username).join(User).where(*conditions)
            total = database.scalar(select(func.count()).select_from(base.subquery()))
            rows = database.execute(base.order_by(
                OperationLog.operated_at.desc(), OperationLog.id.desc(),
            ).limit(20).offset((query.page - 1) * 20)).all()
            return QueryPage([LogView(*row) for row in rows], total, query.page)

    @staticmethod
    def get_detail(log_id: int) -> LogView:
        """Read one complete audit event; no corresponding update or delete operation."""
        UserService.require_admin()
        if not 1 <= log_id <= 9223372036854775807:
            raise NotFound()
        with Session(get_engine()) as database:
            row = database.execute(select(OperationLog, User.username).join(User)
                                   .where(OperationLog.id == log_id)).first()
            if row is None:
                raise NotFound()
            return LogView(*row)
