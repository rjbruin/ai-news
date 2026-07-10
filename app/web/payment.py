"""Lemon Squeezy checkout + webhook routes.

Kept as its own blueprint (rather than folded into ``web`` or ``api``)
because the webhook route is deliberately unauthenticated — verified by an
HMAC signature instead of a login session — and every other route in this
app's blueprints assumes @login_required. Keeping the one exception in its
own file makes "which routes need auth" scannable per-blueprint.
"""
from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import LemonsqueezyProduct
from ..services import balance, payment

bp = Blueprint("payment", __name__, url_prefix="/payment")


@bp.route("/checkout", methods=["POST"])
@login_required
def checkout():
    variant_id = (request.form.get("variant_id") or "").strip()
    product = LemonsqueezyProduct.query.filter_by(variant_id=variant_id, active=True).first()
    if product is None:
        flash("That top-up option isn't available right now.", "danger")
        return redirect(url_for("web.api_keys"))
    try:
        checkout_url = payment.create_checkout(current_user, variant_id)
    except payment.PaymentError as exc:
        current_app.logger.error("Lemon Squeezy checkout creation failed: %s", exc)
        flash("Couldn't start checkout — please try again in a moment.", "danger")
        return redirect(url_for("web.api_keys"))
    return redirect(checkout_url)


@bp.route("/webhook", methods=["POST"])
def webhook():
    """Unauthenticated by design — verified by HMAC-SHA256 signature instead
    of a login session, per Lemon Squeezy's webhook signing scheme."""
    secret = current_app.config.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-Signature")
    if not payment.verify_webhook_signature(request.get_data(), signature, secret):
        return "", 400

    event = request.get_json(force=True, silent=True) or {}
    event_name = (
        request.headers.get("X-Event-Name")
        or event.get("meta", {}).get("event_name")
    )
    if event_name != "order_created":
        # Ack anything else (subscription_*, etc.) so Lemon Squeezy doesn't retry.
        return "", 200

    data = event.get("data", {})
    order_id = data.get("id")
    attributes = data.get("attributes", {})
    custom = event.get("meta", {}).get("custom_data", {})
    user_id = custom.get("user_id")
    variant_id = str((attributes.get("first_order_item") or {}).get("variant_id", ""))

    if not user_id or not variant_id or not order_id:
        current_app.logger.warning("Lemon Squeezy webhook missing required fields: %r", event)
        return "", 200

    product = LemonsqueezyProduct.query.filter_by(variant_id=variant_id, active=True).first()
    if product is None:
        current_app.logger.warning(
            "Lemon Squeezy webhook: unknown/inactive variant_id %s", variant_id
        )
        return "", 200

    try:
        balance.credit(
            int(user_id), product.credited_amount_cents, kind="topup",
            ls_order_id=str(order_id), ls_event_id=f"order_created:{order_id}",
            note=product.label,
        )
    except IntegrityError:
        # Duplicate delivery of the same event (Lemon Squeezy is
        # at-least-once) — already credited, ack and move on.
        db.session.rollback()
    return "", 200
