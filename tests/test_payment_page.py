from app.models import LemonsqueezyProduct
from app.services import balance, payment


def _approve(db, user):
    user.approved = True
    db.session.commit()


def test_payment_page_shows_balance(auth_client, db, user):
    _approve(db, user)
    balance.credit(user.id, 1234)

    resp = auth_client.get("/keys")
    assert resp.status_code == 200
    assert b"$12.34" in resp.data


def test_payment_page_shows_only_active_topup_products(auth_client, db, user):
    _approve(db, user)
    db.session.add_all([
        LemonsqueezyProduct(variant_id="1", label="$1 top-up", credited_amount_cents=100),
        LemonsqueezyProduct(variant_id="2", label="$2 top-up", credited_amount_cents=200, active=False),
    ])
    db.session.commit()

    resp = auth_client.get("/keys")
    assert b"$1 top-up" in resp.data
    assert b"$2 top-up" not in resp.data


def test_payment_page_shows_no_topups_message_when_none_configured(auth_client, db, user):
    _approve(db, user)
    resp = auth_client.get("/keys")
    assert b"aren&#39;t available yet" in resp.data or b"aren't available yet" in resp.data


def test_payment_page_shows_transaction_history(auth_client, db, user):
    _approve(db, user)
    balance.credit(user.id, 500, kind="topup", note="$5 top-up")

    resp = auth_client.get("/keys")
    assert b"topup" in resp.data
    assert b"$5 top-up" in resp.data


def test_checkout_redirects_to_lemonsqueezy(auth_client, db, user, monkeypatch):
    _approve(db, user)
    db.session.add(LemonsqueezyProduct(variant_id="123", label="$5 top-up", credited_amount_cents=500))
    db.session.commit()

    monkeypatch.setattr(payment, "create_checkout", lambda u, variant_id: "https://checkout.lemonsqueezy.com/fake")

    resp = auth_client.post("/payment/checkout", data={"variant_id": "123"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://checkout.lemonsqueezy.com/fake"


def test_checkout_rejects_unknown_variant(auth_client, db, user):
    _approve(db, user)
    resp = auth_client.post("/payment/checkout", data={"variant_id": "does-not-exist"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"isn&#39;t available" in resp.data or b"isn't available" in resp.data


def test_checkout_requires_login(client, db):
    resp = client.post("/payment/checkout", data={"variant_id": "123"})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
