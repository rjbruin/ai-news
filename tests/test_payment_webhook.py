import hashlib
import hmac
import json

from app.models import BalanceTransaction, LemonsqueezyProduct

WEBHOOK_SECRET = "test-webhook-secret"  # matches TestConfig.LEMONSQUEEZY_WEBHOOK_SECRET


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _order_created_payload(*, order_id="1", variant_id="123", user_id):
    return {
        "meta": {
            "event_name": "order_created",
            "custom_data": {"user_id": str(user_id)},
        },
        "data": {
            "type": "orders",
            "id": str(order_id),
            "attributes": {
                "status": "paid",
                "first_order_item": {"variant_id": int(variant_id)},
            },
        },
    }


def _post_webhook(client, payload: dict, *, event_name="order_created", bad_signature=False):
    body = json.dumps(payload).encode()
    signature = "0" * 64 if bad_signature else _sign(body)
    return client.post(
        "/payment/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature,
            "X-Event-Name": event_name,
        },
    )


def test_webhook_credits_balance_on_order_created(client, db, user):
    db.session.add(LemonsqueezyProduct(variant_id="123", label="$5 top-up", credited_amount_cents=500))
    db.session.commit()

    resp = _post_webhook(client, _order_created_payload(user_id=user.id))
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 500

    row = BalanceTransaction.query.filter_by(user_id=user.id).first()
    assert row.kind == "topup"
    assert row.ls_order_id == "1"


def test_webhook_is_idempotent_on_redelivery(client, db, user):
    db.session.add(LemonsqueezyProduct(variant_id="123", label="$5 top-up", credited_amount_cents=500))
    db.session.commit()

    payload = _order_created_payload(user_id=user.id)
    resp1 = _post_webhook(client, payload)
    resp2 = _post_webhook(client, payload)
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    db.session.refresh(user)
    assert user.balance_cents == 500  # not 1000
    assert BalanceTransaction.query.filter_by(user_id=user.id).count() == 1


def test_webhook_rejects_bad_signature(client, db, user):
    db.session.add(LemonsqueezyProduct(variant_id="123", label="$5 top-up", credited_amount_cents=500))
    db.session.commit()

    resp = _post_webhook(client, _order_created_payload(user_id=user.id), bad_signature=True)
    assert resp.status_code == 400
    db.session.refresh(user)
    assert user.balance_cents == 0


def test_webhook_ignores_unknown_variant(client, db, user):
    resp = _post_webhook(client, _order_created_payload(user_id=user.id, variant_id="999"))
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 0


def test_webhook_ignores_inactive_product(client, db, user):
    db.session.add(LemonsqueezyProduct(
        variant_id="123", label="Inactive", credited_amount_cents=500, active=False,
    ))
    db.session.commit()

    resp = _post_webhook(client, _order_created_payload(user_id=user.id))
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 0


def test_webhook_ignores_non_order_created_events(client, db, user):
    db.session.add(LemonsqueezyProduct(variant_id="123", label="$5 top-up", credited_amount_cents=500))
    db.session.commit()

    resp = _post_webhook(
        client, _order_created_payload(user_id=user.id), event_name="subscription_created",
    )
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 0
