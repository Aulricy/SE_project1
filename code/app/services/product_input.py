"""Validation and typed input for product editing and combination queries."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from werkzeug.datastructures import FileStorage

from app.models.product import ProductCategory, ProductStatus


class ProductError(Exception):
    """A safe product validation or stale-state error."""

    def __init__(self, message: str, status_code: int = 400, field: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.field = field


def parse_price(value: str, *, allow_zero: bool = False) -> int | None:
    """Parse exact decimal cents; supplied draft prices must also be valid."""
    value = str(value).strip()
    if not value:
        return None
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value) or len(value) > 12:
        raise ProductError("价格须大于 0，最多两位小数，且不超过 99999999.99 元。", field="price")
    try:
        price = Decimal(value)
    except InvalidOperation as error:
        raise ProductError("请输入有效价格。", field="price") from error
    if price > Decimal("99999999.99") or price < 0 or (not allow_zero and price == 0):
        raise ProductError("价格须大于 0，且不超过 99999999.99 元。", field="price")
    return int(price * 100)


@dataclass
class ProductInput:
    """The only seller-controlled fields; ownership, status and version are not editable."""

    name: str = ""
    category: str = ""
    price: str = ""
    description: str = ""
    image: FileStorage | None = None
    remove_image: bool = False

    def validated(self, *, complete: bool = False) -> dict:
        """Allow incomplete drafts but require all publication fields when submitting."""
        values = {
            "name": self.name.strip() or None, "description": self.description.strip() or None,
        }
        for field, limit in (("name", 100), ("description", 5000)):
            if values[field] and len(values[field]) > limit:
                raise ProductError(f"内容最多 {limit} 个字符。", field=field)
        try:
            values["category"] = ProductCategory(self.category) if self.category else None
        except ValueError as error:
            raise ProductError("请选择提供的固定分类。", field="category") from error
        values["price_cents"] = parse_price(self.price)
        if complete:
            for field, label in (
                ("name", "商品名称"), ("category", "分类"),
                ("price_cents", "价格"), ("description", "商品描述"),
            ):
                if values[field] is None:
                    raise ProductError(
                        f"提交审核前请填写{label}。", field="price" if field == "price_cents" else field,
                    )
        if self.image and self.remove_image:
            raise ProductError("上传新图片与移除图片不能同时选择。", field="image")
        return values


@dataclass(frozen=True)
class ProductQuery:
    """Whitelisted, validated SQL query parameters."""

    keyword: str = ""
    category: ProductCategory | None = None
    min_price: int | None = None
    max_price: int | None = None
    status: ProductStatus | None = None
    page: int = 1
    per_page: int = 12

    @classmethod
    def from_mapping(cls, values) -> "ProductQuery":
        """Reject invalid filters instead of silently changing the user's query."""
        try:
            category = ProductCategory(values["category"]) if values.get("category") else None
            status = ProductStatus(values["status"]) if values.get("status") else None
            page = int(values.get("page", "1"))
        except (ValueError, TypeError) as error:
            raise ProductError("分类、状态或页码无效。") from error
        minimum = parse_price(values.get("min_price", ""), allow_zero=True)
        maximum = parse_price(values.get("max_price", ""), allow_zero=True)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ProductError("价格下限不能大于上限。")
        keyword = values.get("keyword", "").strip()
        if len(keyword) > 100 or not 1 <= page <= 100000:
            raise ProductError("关键词最多 100 个字符，页码须为 1–100000。")
        return cls(keyword, category, minimum, maximum, status, page)
