"""Buyer booking, seller confirmation, cancellation, completion and personal history."""

from uuid import uuid4

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from werkzeug.exceptions import NotFound

from app.models import ReservationStatus
from app.services.product_images import image_response
from app.services.reservation_input import ReservationError, ReservationQuery, positive_version
from app.services.reservation_queries import ReservationPage, ReservationQueries
from app.services.reservations import ReservationService
from app.services.users import UserService

blueprint = Blueprint("reservations", __name__)


@blueprint.post("/products/<int:product_id>/reserve")
def reserve(product_id: int):
    """Identity and captured price come from the server, not from posted buyer/price fields."""
    UserService.require_member()
    result = ReservationService.reserve(
        product_id, positive_version(request.form.get("version")),
        request.form.get("request_key", ""),
    )
    flash("此请求已处理，显示原预约的当前状态。" if result.replayed else "预约成功，等待卖家确认。", "success")
    return redirect(url_for("reservations.detail", reservation_id=result.reservation.id), code=303)


def _render_detail(reservation_id: int, message=None, status_code=200, submitted=None):
    """Retain a failed command's original key, version and text instead of upgrading it."""
    record = ReservationQueries.get_detail(reservation_id)
    forms = {
        operation: {
            "request_key": uuid4().hex, "version": str(record.reservation.version), "reason": "",
        }
        for operation in ("confirm", "cancel", "complete")
    }
    if submitted is not None and submitted["operation"] in forms:
        forms[submitted["operation"]] = submitted
    return render_template(
        "reservations/detail.html", record=record, reservation=record.reservation,
        product=record.product, forms=forms, form_error=message,
        ReservationStatus=ReservationStatus,
        is_buyer=g.user.id == record.reservation.buyer_id,
        is_seller=g.user.id == record.product.seller_id,
        can_view_current_image=record.may_view_image(g.user.id),
    ), status_code


@blueprint.get("/reservations/<int:reservation_id>")
def detail(reservation_id: int):
    """Show prices, contact text, processing reasons and timestamps only to participants."""
    return _render_detail(reservation_id)


@blueprint.get("/reservations/<int:reservation_id>/image")
def image(reservation_id: int):
    """A buyer can read the related image even after the product leaves public listings."""
    record = ReservationQueries.get_detail(reservation_id)
    if not record.may_view_image(g.user.id):
        raise NotFound()
    return image_response(record.product.image_path)


@blueprint.post("/reservations/<int:reservation_id>/actions/<operation>")
def action(reservation_id: int, operation: str):
    """Each accepted action has an actor-scoped key and the exact original page version."""
    UserService.require_member()
    if operation not in {"confirm", "cancel", "complete"}:
        raise ReservationError("不支持的预约操作。", 404)
    submitted = {
        "operation": operation, "request_key": request.form.get("request_key", ""),
        "version": request.form.get("version", ""), "reason": request.form.get("reason", ""),
    }
    try:
        arguments = (
            reservation_id, positive_version(submitted["version"]), submitted["request_key"],
        )
        if operation == "cancel":
            result = ReservationService.cancel(*arguments, submitted["reason"])
        elif operation == "confirm":
            result = ReservationService.confirm(*arguments)
        else:
            result = ReservationService.complete(*arguments)
    except ReservationError as error:
        return _render_detail(reservation_id, str(error), error.status_code, submitted)
    flash("此请求已处理，显示原预约的当前状态。" if result.replayed else "预约操作成功。", "success")
    return redirect(url_for("reservations.detail", reservation_id=result.reservation.id), code=303)


@blueprint.get("/reservations")
def list_records():
    """Browse buying/selling history, including cancelled and completed reservations."""
    UserService.require_member()
    values = request.args.to_dict()
    message, status_code = None, 200
    try:
        query = ReservationQuery.from_mapping(values)
        page = ReservationQueries.search(query)
    except ReservationError as error:
        side = values.get("side", "buying")
        query = ReservationQuery(side=side if side in {"buying", "selling"} else "buying")
        page = ReservationPage([], 0, 1)
        message, status_code = str(error), error.status_code
    parameters = {
        "side": query.side, "status": values.get("status", ""),
        "product_id": values.get("product_id", ""),
    }
    previous_url = (
        url_for("reservations.list_records", **parameters, page=page.page - 1)
        if page.page > 1 else None
    )
    next_url = (
        url_for("reservations.list_records", **parameters, page=page.page + 1)
        if page.page < page.pages else None
    )
    return render_template(
        "reservations/list.html", page=page, query=query, values=values,
        status_options=[(status.value, status.label) for status in ReservationStatus],
        query_error=message, previous_url=previous_url, next_url=next_url,
    ), status_code


def init_app(app):
    """Register routes and safe booking error messages."""
    app.register_blueprint(blueprint)

    def reservation_error(error):
        """Failed booking does not silently resubmit against a newer product version."""
        return render_template(
            "error.html", title="预约操作未完成", message=str(error),
        ), error.status_code

    app.register_error_handler(ReservationError, reservation_error)
