"""Prepaid balance ledger: race-safe credit/debit against User.balance_cents.

Costs elsewhere in the app are USD floats (ApiKeyUsage.cost, SummaryRun.
agent_cost/podcast_cost); the balance itself is stored as whole cents to
avoid float-rounding drift. debit() rounds a USD cost up to the next cent so
the house never under-charges by a fraction of a cent.

debit()/credit() use a conditional UPDATE (guarded in the WHERE clause)
rather than read-then-write, since ingestion polling runs on a background
scheduler thread and could otherwise race with request-handling threads
debiting the same user concurrently.
"""
from __future__ import annotations

import math

from ..extensions import db
from ..models import BalanceTransaction, User


class InsufficientBalance(RuntimeError):
    """Raised by debit() when the balance doesn't cover the requested amount.
    Nothing is deducted when this is raised."""


def debit(
    user_id: int,
    amount_usd: float,
    *,
    kind: str = "spend",
    source_id: int | None = None,
    summary_run_id: int | None = None,
    usage_kind: str | None = None,
) -> None:
    """Atomically deduct ``amount_usd`` (rounded up to whole cents) from
    user_id's balance, or raise InsufficientBalance without deducting
    anything. Commits its own transaction."""
    cents = math.ceil(amount_usd * 100)
    if cents <= 0:
        return
    result = db.session.execute(
        db.update(User)
        .where(User.id == user_id, User.balance_cents >= cents)
        .values(balance_cents=User.balance_cents - cents)
    )
    if result.rowcount == 0:
        db.session.rollback()
        raise InsufficientBalance(f"Balance too low to cover ${amount_usd:.4f}.")
    new_balance = db.session.execute(
        db.select(User.balance_cents).where(User.id == user_id)
    ).scalar_one()
    db.session.add(BalanceTransaction(
        user_id=user_id, kind=kind, amount_cents=-cents,
        balance_after_cents=new_balance, source_id=source_id,
        summary_run_id=summary_run_id, usage_kind=usage_kind,
    ))
    db.session.commit()


def credit(
    user_id: int,
    amount_cents: int,
    *,
    kind: str = "topup",
    ls_order_id: str | None = None,
    ls_event_id: str | None = None,
    note: str | None = None,
    created_by_user_id: int | None = None,
) -> None:
    """Atomically add amount_cents (may be negative, for admin corrections)
    to user_id's balance and record a ledger row. Commits its own
    transaction.

    Idempotency for webhook redelivery: ls_event_id has a unique index on
    BalanceTransaction — callers processing a Lemon Squeezy webhook should
    catch IntegrityError and treat it as "already processed" rather than
    double-crediting."""
    db.session.execute(
        db.update(User).where(User.id == user_id)
        .values(balance_cents=User.balance_cents + amount_cents)
    )
    new_balance = db.session.execute(
        db.select(User.balance_cents).where(User.id == user_id)
    ).scalar_one()
    db.session.add(BalanceTransaction(
        user_id=user_id, kind=kind, amount_cents=amount_cents,
        balance_after_cents=new_balance, ls_order_id=ls_order_id,
        ls_event_id=ls_event_id, note=note, created_by_user_id=created_by_user_id,
    ))
    db.session.commit()


def has_sufficient(user_id: int, amount_usd: float) -> bool:
    """Pre-flight check only — not itself race-safe under concurrent debits,
    always pair with debit()'s atomic guard for the actual charge. Useful
    for UX (e.g. disabling a "fund from balance" option at $0)."""
    cents = math.ceil(amount_usd * 100)
    balance = db.session.execute(
        db.select(User.balance_cents).where(User.id == user_id)
    ).scalar_one()
    return balance >= cents
