"""Product editing, review, private images, and scoped catalogue pages."""

from uuid import uuid4

from flask import (
    Blueprint, flash, g, redirect, render_template, request, url_for,
)
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from werkzeug.exceptions import Forbidden
from wtforms import BooleanField, HiddenField, SelectChoice, SelectField, StringField, TextAreaField
from wtforms.validators import InputRequired, Length

from app.identifiers import positive_integer
from app.models import UserRole
from app.models.product import ProductStatus
from app.services.product_images import image_response
from app.services.product_input import ProductError, ProductInput, ProductQuery
from app.services.products import CATEGORY_OPTIONS, STATUS_OPTIONS, ProductPage, ProductService
from app.services.users import UserService

blueprint = Blueprint("products", __name__)


class ProductForm(FlaskForm):
    """Allow incomplete drafts; publication validation also lives in the service."""

    name = StringField("商品名称", validators=[Length(max=100, message="商品名称最多 100 个字符。")])
    category = SelectField(
        "分类", choices=[SelectChoice("", "请选择分类")]
        + [SelectChoice(value, label) for value, label in CATEGORY_OPTIONS],
    )
    price = StringField("价格（元）", validators=[Length(max=12, message="价格输入过长。")])
    description = TextAreaField(
        "商品描述", validators=[Length(max=5000, message="商品描述最多 5000 个字符。")],
    )
    image = FileField("商品图片（可选）")
    remove_image = BooleanField("移除现有图片")
    version = HiddenField("商品版本", default="1", validators=[InputRequired("商品版本无效。")])


def _render_list(scope: str):
    """Keep list parameters and pagination within their authorized scope."""
    values = request.args.to_dict()
    if scope == "admin" and "status" not in values:
        values["status"] = ProductStatus.PENDING_REVIEW.value
    message = None
    status = 200
    try:
        query = ProductQuery.from_mapping(values)
        page = ProductService.search(query, scope)
    except ProductError as error:
        message, status = str(error), error.status_code
        query, page = ProductQuery(), ProductPage([], 0, 1, 12)
    endpoints = {"public": "products.catalog", "own": "products.own", "admin": "products.admin"}
    parameters = {
        key: values.get(key, "")
        for key in ("keyword", "category", "min_price", "max_price", "status")
    }
    previous_url = (
        url_for(endpoints[scope], **parameters, page=page.page - 1)
        if page.page > 1 else None
    )
    next_url = (
        url_for(endpoints[scope], **parameters, page=page.page + 1)
        if page.page < page.pages else None
    )
    return render_template(
        "products/list.html", page=page, scope=scope, values=values, query_error=message,
        category_options=CATEGORY_OPTIONS, status_options=STATUS_OPTIONS,
        previous_url=previous_url, next_url=next_url,
    ), status


@blueprint.get("/products")
def catalog():
    """Public browsing only returns published products regardless of supplied filters."""
    return _render_list("public")


@blueprint.get("/my/products")
def own():
    """List only the signed-in ordinary user's products."""
    UserService.require_member()
    return _render_list("own")


@blueprint.get("/admin/products")
def admin():
    """Both kinds of administrator may review and query all products."""
    UserService.require_admin()
    return _render_list("admin")


@blueprint.get("/products/<int:product_id>")
def detail(product_id: int):
    """Apply current product visibility to the detail view."""
    return _detail(product_id)


def _detail(product_id: int, **context):
    """Apply detail visibility before rendering anything from an unpublished product."""
    record = ProductService.get_detail(product_id)
    actor = g.get("user")
    is_owner = bool(actor and actor.id == record.product.seller_id)
    return render_template(
        "products/detail.html", record=record, product=record.product, is_owner=is_owner,
        can_review=bool(actor and actor.role == UserRole.ADMIN),
        ProductStatus=ProductStatus, reserve_key=uuid4().hex, **context,
    )


@blueprint.get("/products/<int:product_id>/image")
def image(product_id: int):
    """Protect images using the same current product visibility as the details page."""
    product = ProductService.get_detail(product_id).product
    return image_response(product.image_path)


def _payload(form: ProductForm) -> ProductInput:
    """Whitelist only editable fields and enforce one uploaded file."""
    uploads = [upload for upload in request.files.getlist("image") if upload.filename]
    if len(uploads) > 1:
        raise ProductError("一件商品最多上传一张图片。", field="image")
    return ProductInput(
        form.name.data or "", form.category.data or "", form.price.data or "",
        form.description.data or "", uploads[0] if uploads else None, form.remove_image.data,
    )


def _version(value: str) -> int:
    """Reject absent or malformed optimistic concurrency versions."""
    try:
        version = positive_integer(value)
    except (TypeError, ValueError) as error:
        raise ProductError("商品版本无效，请刷新后重试。") from error
    return version


def _editable_product(product_id: int | None, actor):
    """Check editor visibility and ownership without mixing it with form processing."""
    if product_id is None:
        return None
    product = ProductService.get_detail(product_id).product
    if product.seller_id != actor.id:
        raise Forbidden()
    if request.method == "GET" and product.status != ProductStatus.DRAFT:
        raise ProductError("该商品当前不可直接编辑，请先按状态规则退回草稿。", 409)
    return product


def _editor(product_id: int | None):
    """Preserve input and the original version after validation or concurrency failure."""
    actor = UserService.require_member()
    product = _editable_product(product_id, actor)
    defaults = {}
    if product is not None:
        defaults = {
            "name": product.name, "category": product.category.value if product.category else "",
            "price": f"{product.price:.2f}" if product.price is not None else "",
            "description": product.description, "version": str(product.version),
        }
    form = ProductForm(data=defaults)
    if product is None:
        form.version.validators = []
    message = None
    status = 400 if request.method == "POST" else 200
    if form.validate_on_submit():
        try:
            intent = request.form.get("intent", "save")
            if intent not in {"save", "submit"}:
                raise ProductError("不支持的保存方式。")
            payload = _payload(form)
            if product is None:
                saved = ProductService.create_draft(payload, submit=intent == "submit")
            else:
                saved = ProductService.update_draft(
                    product.id, _version(form.version.data), payload, submit=intent == "submit",
                )
        except ProductError as error:
            status = error.status_code
            if error.field and error.field in form:
                form[error.field].errors = [str(error)]
            else:
                message = str(error)
        else:
            flash("已提交审核。" if intent == "submit" else "草稿已保存。", "success")
            return redirect(url_for("products.detail", product_id=saved.id), code=303)
    return render_template(
        "products/edit.html", form=form, product=product, form_error=message,
    ), status


@blueprint.route("/products/new", methods=["GET", "POST"])
def create():
    """Create an incomplete draft or submit a complete new product."""
    return _editor(None)


@blueprint.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def edit(product_id: int):
    """Edit a draft belonging to the signed-in seller."""
    return _editor(product_id)


@blueprint.post("/products/<int:product_id>/actions/<operation>")
def action(product_id: int, operation: str):
    """Every state change checks role, ownership, state, version and CSRF on the server."""
    try:
        ProductService.transition(
            product_id, _version(request.form.get("version")),
            operation, request.form.get("reason", ""),
        )
    except ProductError as error:
        return _detail(
            product_id, action_error=str(error), failed_operation=operation,
            action_version=request.form.get("version", ""),
            action_reason=request.form.get("reason", ""),
        ), error.status_code
    flash("商品操作成功。", "success")
    return redirect(url_for("products.detail", product_id=product_id), code=303)


def init_app(app):
    """Register product routes and uniform stale/validation error responses."""
    app.register_blueprint(blueprint)

    def product_error(error):
        """Render a safe message and require an explicit refresh after conflicts."""
        return render_template("products/error.html", message=str(error)), error.status_code

    app.register_error_handler(ProductError, product_error)
