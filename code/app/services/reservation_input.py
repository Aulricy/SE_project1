"""Validated command identity and reservation list filters."""

from dataclasses import dataclass
import hashlib
import json
import re

from app.identifiers import positive_integer
from app.models.reservation import ReservationStatus


class ReservationError(Exception):
    """A safe, user-visible validation or concurrency conflict."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def positive_version(value) -> int:
    """Never replace a missing or stale client version with a current database version."""
    try:
        number = positive_integer(value)
    except (ValueError, TypeError) as error:
        raise ReservationError("页面版本无效，请刷新后重试。") from error
    return number


@dataclass(frozen=True)
class ReservationCommand:
    """Fingerprint the original accepted parameters, including the original page version."""

    operation: str
    target_id: int
    expected_version: int
    request_key: str
    reason: str = ""

    def validate(self) -> None:
        """Bound all accepted input before any database write."""
        if self.operation not in {"reserve", "confirm", "cancel", "complete"}:
            raise ReservationError("不支持的预约操作。", 404)
        positive_version(self.expected_version)
        if not isinstance(self.request_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{16,128}", self.request_key,
        ):
            raise ReservationError("请求标识无效，请刷新页面后重试。")
        if len(self.reason) > 1000:
            raise ReservationError("取消原因最多 1000 个字符。")

    @property
    def fingerprint(self) -> str:
        """Distinguish changed content without storing raw request payloads."""
        canonical = json.dumps({
            "operation": self.operation, "target_id": self.target_id,
            "expected_version": self.expected_version, "reason": self.reason,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReservationQuery:
    """Participant-scoped history filters and fixed-size pagination."""

    side: str = "buying"
    status: ReservationStatus | None = None
    product_id: int | None = None
    page: int = 1
    per_page: int = 12

    @classmethod
    def from_mapping(cls, values):
        """Validate filters without accepting a buyer or seller identity from the client."""
        side = values.get("side", "buying")
        if side not in {"buying", "selling"}:
            raise ReservationError("请选择买入记录或卖出记录。")
        try:
            status = ReservationStatus(values["status"]) if values.get("status") else None
            page = int(values.get("page", "1"))
            product_id = (positive_integer(values["product_id"])
                          if values.get("product_id") else None)
        except (ValueError, TypeError) as error:
            raise ReservationError("预约状态、商品编号或页码无效。") from error
        if not 1 <= page <= 100000 or (product_id is not None and product_id < 1):
            raise ReservationError("商品编号和页码须为有效正整数。")
        return cls(side, status, product_id, page)
