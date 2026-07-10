from app.models import LemonsqueezyProduct


def test_admin_can_create_product(admin_client, db):
    resp = admin_client.post(
        "/admin/lemonsqueezy-products/new",
        data={"variant_id": "123", "label": "$5 top-up", "credited_amount_dollars": "5.00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    product = LemonsqueezyProduct.query.filter_by(variant_id="123").first()
    assert product is not None
    assert product.label == "$5 top-up"
    assert product.credited_amount_cents == 500
    assert product.active is True


def test_admin_cannot_create_duplicate_variant_id(admin_client, db):
    db.session.add(LemonsqueezyProduct(variant_id="dupe", label="Existing", credited_amount_cents=100))
    db.session.commit()

    resp = admin_client.post(
        "/admin/lemonsqueezy-products/new",
        data={"variant_id": "dupe", "label": "Second", "credited_amount_dollars": "1.00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert LemonsqueezyProduct.query.filter_by(variant_id="dupe").count() == 1


def test_admin_create_rejects_missing_fields(admin_client, db):
    resp = admin_client.post(
        "/admin/lemonsqueezy-products/new",
        data={"variant_id": "", "label": "", "credited_amount_dollars": "0"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert LemonsqueezyProduct.query.count() == 0


def test_non_admin_cannot_create_product(auth_client, db):
    resp = auth_client.post(
        "/admin/lemonsqueezy-products/new",
        data={"variant_id": "1", "label": "x", "credited_amount_dollars": "1"},
    )
    assert resp.status_code == 403


def test_admin_can_toggle_product(admin_client, db):
    product = LemonsqueezyProduct(variant_id="t1", label="Toggle me", credited_amount_cents=100)
    db.session.add(product)
    db.session.commit()

    resp = admin_client.post(f"/admin/lemonsqueezy-products/{product.id}/toggle", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(product)
    assert product.active is False

    admin_client.post(f"/admin/lemonsqueezy-products/{product.id}/toggle", follow_redirects=True)
    db.session.refresh(product)
    assert product.active is True


def test_admin_can_delete_product(admin_client, db):
    product = LemonsqueezyProduct(variant_id="d1", label="Delete me", credited_amount_cents=100)
    db.session.add(product)
    db.session.commit()
    product_id = product.id

    resp = admin_client.post(f"/admin/lemonsqueezy-products/{product_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(LemonsqueezyProduct, product_id) is None


def test_admin_can_adjust_user_balance(admin_client, db, user):
    resp = admin_client.post(
        f"/admin/users/{user.id}/adjust-balance",
        data={"amount_dollars": "10.00", "reason": "test credit"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 1000


def test_admin_can_adjust_user_balance_negative(admin_client, db, user):
    from app.services import balance

    balance.credit(user.id, 1000)
    resp = admin_client.post(
        f"/admin/users/{user.id}/adjust-balance",
        data={"amount_dollars": "-3.00", "reason": "correction"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.balance_cents == 700


def test_non_admin_cannot_adjust_balance(auth_client, db, user):
    resp = auth_client.post(
        f"/admin/users/{user.id}/adjust-balance",
        data={"amount_dollars": "10.00"},
    )
    assert resp.status_code == 403
