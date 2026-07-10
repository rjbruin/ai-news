import pytest

from app.models import BalanceTransaction
from app.services import balance


def test_credit_increases_balance_and_records_transaction(db, user):
    balance.credit(user.id, 500, kind="topup", ls_order_id="1", ls_event_id="order_created:1", note="$5 top-up")
    db.session.refresh(user)
    assert user.balance_cents == 500

    row = BalanceTransaction.query.filter_by(user_id=user.id).first()
    assert row.kind == "topup"
    assert row.amount_cents == 500
    assert row.balance_after_cents == 500
    assert row.ls_order_id == "1"
    assert row.note == "$5 top-up"


def test_debit_decreases_balance_when_sufficient(db, user):
    balance.credit(user.id, 1000)
    balance.debit(user.id, 2.50, kind="spend", usage_kind="ingest")
    db.session.refresh(user)
    assert user.balance_cents == 750

    row = BalanceTransaction.query.filter_by(user_id=user.id, kind="spend").first()
    assert row.amount_cents == -250
    assert row.balance_after_cents == 750
    assert row.usage_kind == "ingest"


def test_debit_rounds_cost_up_to_the_next_cent(db, user):
    balance.credit(user.id, 100)
    balance.debit(user.id, 0.001)  # a tenth of a cent — must round up to 1 cent, not truncate to 0
    db.session.refresh(user)
    assert user.balance_cents == 99


def test_debit_raises_and_deducts_nothing_when_insufficient(db, user):
    balance.credit(user.id, 100)
    with pytest.raises(balance.InsufficientBalance):
        balance.debit(user.id, 5.00)
    db.session.refresh(user)
    assert user.balance_cents == 100  # unchanged
    assert BalanceTransaction.query.filter_by(user_id=user.id, kind="spend").count() == 0


def test_debit_exact_balance_succeeds_and_zeroes_out(db, user):
    balance.credit(user.id, 250)
    balance.debit(user.id, 2.50)
    db.session.refresh(user)
    assert user.balance_cents == 0


def test_sequential_debits_exhausting_a_balance(db, user):
    balance.credit(user.id, 300)
    balance.debit(user.id, 1.00)
    balance.debit(user.id, 1.00)
    db.session.refresh(user)
    assert user.balance_cents == 100
    with pytest.raises(balance.InsufficientBalance):
        balance.debit(user.id, 2.00)
    db.session.refresh(user)
    assert user.balance_cents == 100


def test_negative_credit_allows_admin_debit_below_zero(db, user):
    balance.credit(user.id, 100)
    balance.credit(user.id, -300, kind="adjustment", note="support correction")
    db.session.refresh(user)
    assert user.balance_cents == -200


def test_has_sufficient(db, user):
    balance.credit(user.id, 100)
    assert balance.has_sufficient(user.id, 1.00) is True
    assert balance.has_sufficient(user.id, 1.01) is False


def test_debit_of_zero_or_negative_cost_is_a_noop(db, user):
    balance.credit(user.id, 100)
    balance.debit(user.id, 0)
    balance.debit(user.id, -5)
    db.session.refresh(user)
    assert user.balance_cents == 100
