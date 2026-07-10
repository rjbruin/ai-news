"""Lemon Squeezy API client: checkout creation and webhook signature
verification.

Signature scheme confirmed against Lemon Squeezy's docs (docs.lemonsqueezy.
com/help/webhooks/signing-requests): the raw request body is HMAC-SHA256'd
with the webhook signing secret, hex-encoded, and sent in the X-Signature
header. The event name arrives in the X-Event-Name header (also echoed in
the body's meta.event_name). Custom checkout data (e.g. our user id) is
echoed back under meta.custom_data. An order_created payload's variant id
lives at data.attributes.first_order_item.variant_id, and the order's own
unique id is data.id — used as the idempotency key since Lemon Squeezy has
no dedicated delivery-id header.
"""
from __future__ import annotations

import hashlib
import hmac

import httpx
from flask import current_app

LEMONSQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"


class PaymentError(RuntimeError):
    """Raised on a Lemon Squeezy API or configuration failure."""


def is_configured() -> bool:
    return bool(
        current_app.config.get("LEMONSQUEEZY_API_KEY")
        and current_app.config.get("LEMONSQUEEZY_STORE_ID")
    )


def create_checkout(user, variant_id: str) -> str:
    """Create a Lemon Squeezy checkout for ``user``, embedding user.id in
    checkout_data.custom so the webhook can credit the right account without
    ever trusting a client-supplied amount. Returns the checkout URL."""
    if not is_configured():
        raise PaymentError("Lemon Squeezy is not configured.")
    api_key = current_app.config["LEMONSQUEEZY_API_KEY"]
    store_id = current_app.config["LEMONSQUEEZY_STORE_ID"]
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {"custom": {"user_id": str(user.id)}},
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(store_id)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }
    try:
        resp = httpx.post(
            f"{LEMONSQUEEZY_API_BASE}/checkouts",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            },
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise PaymentError(
            f"Lemon Squeezy HTTP {exc.response.status_code}: {exc.response.text[:500]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise PaymentError(f"Lemon Squeezy request failed: {exc}") from exc

    return resp.json()["data"]["attributes"]["url"]


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """HMAC-SHA256 hex-digest constant-time comparison, per Lemon Squeezy's
    documented webhook signing scheme."""
    if not signature_header or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature_header)
