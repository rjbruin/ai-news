"""remove payment/balance features (BYOK only)

The prepaid-balance/Lemon Squeezy payment system was built through Phase 3
of a planned 4-phase rollout but Phase 4 (actually spending the balance)
was never built — balance.debit()/has_sufficient() were dead code. The app
is now BYOK-only: every user pays for their own usage with their own
OpenRouter key. Confirmed zero nonzero balances, zero balance_transactions,
and zero lemonsqueezy_products rows in production before writing this.

Revision ID: b8dfc16f8f94
Revises: e76cde819e11
Create Date: 2026-07-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8dfc16f8f94'
down_revision = 'e76cde819e11'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('balance_transactions')
    op.drop_table('lemonsqueezy_products')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('balance_cents')


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('balance_cents', sa.Integer(), nullable=False, server_default='0')
        )
    op.create_table(
        'lemonsqueezy_products',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('variant_id', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('credited_amount_cents', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index(
        'ix_lemonsqueezy_products_variant_id', 'lemonsqueezy_products', ['variant_id'], unique=True,
    )
    op.create_table(
        'balance_transactions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('balance_after_cents', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('sources.id'), nullable=True),
        sa.Column('summary_run_id', sa.Integer(), sa.ForeignKey('summary_runs.id'), nullable=True),
        sa.Column('usage_kind', sa.String(length=20), nullable=True),
        sa.Column('ls_order_id', sa.String(length=64), nullable=True),
        sa.Column('ls_event_id', sa.String(length=64), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_balance_transactions_user_id', 'balance_transactions', ['user_id'])
    op.create_index('ix_balance_transactions_source_id', 'balance_transactions', ['source_id'])
    op.create_index('ix_balance_transactions_summary_run_id', 'balance_transactions', ['summary_run_id'])
    op.create_index('ix_balance_transactions_ls_order_id', 'balance_transactions', ['ls_order_id'])
    op.create_index(
        'ix_balance_transactions_ls_event_id', 'balance_transactions', ['ls_event_id'], unique=True,
    )
