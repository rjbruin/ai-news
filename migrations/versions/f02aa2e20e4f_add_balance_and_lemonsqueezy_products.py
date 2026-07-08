"""add balance_transactions, lemonsqueezy_products, users.balance_cents

Adds the prepaid-balance ledger and Lemon Squeezy top-up product mapping
(Phases 1-3 of the balance feature — see TODO_PAYMENT_PHASE4.md for what's
still left to wire in before a balance is actually spendable).

Revision ID: f02aa2e20e4f
Revises: 117aa3fc3b06
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f02aa2e20e4f'
down_revision = '117aa3fc3b06'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('balance_cents', sa.Integer(), nullable=False, server_default='0')
        )

    op.create_table(
        'lemonsqueezy_products',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('variant_id', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('credited_amount_cents', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('variant_id'),
    )
    with op.batch_alter_table('lemonsqueezy_products', schema=None) as batch_op:
        batch_op.create_index(
            'ix_lemonsqueezy_products_variant_id', ['variant_id'], unique=True,
        )

    op.create_table(
        'balance_transactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('balance_after_cents', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('summary_run_id', sa.Integer(), nullable=True),
        sa.Column('usage_kind', sa.String(length=20), nullable=True),
        sa.Column('ls_order_id', sa.String(length=64), nullable=True),
        sa.Column('ls_event_id', sa.String(length=64), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
        sa.ForeignKeyConstraint(['summary_run_id'], ['summary_runs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ls_event_id'),
    )
    with op.batch_alter_table('balance_transactions', schema=None) as batch_op:
        batch_op.create_index(
            'ix_balance_transactions_user_id', ['user_id'], unique=False,
        )
        batch_op.create_index(
            'ix_balance_transactions_source_id', ['source_id'], unique=False,
        )
        batch_op.create_index(
            'ix_balance_transactions_summary_run_id', ['summary_run_id'], unique=False,
        )
        batch_op.create_index(
            'ix_balance_transactions_ls_order_id', ['ls_order_id'], unique=False,
        )
        batch_op.create_index(
            'ix_balance_transactions_ls_event_id', ['ls_event_id'], unique=True,
        )


def downgrade():
    with op.batch_alter_table('balance_transactions', schema=None) as batch_op:
        batch_op.drop_index('ix_balance_transactions_ls_event_id')
        batch_op.drop_index('ix_balance_transactions_ls_order_id')
        batch_op.drop_index('ix_balance_transactions_summary_run_id')
        batch_op.drop_index('ix_balance_transactions_source_id')
        batch_op.drop_index('ix_balance_transactions_user_id')
    op.drop_table('balance_transactions')

    with op.batch_alter_table('lemonsqueezy_products', schema=None) as batch_op:
        batch_op.drop_index('ix_lemonsqueezy_products_variant_id')
    op.drop_table('lemonsqueezy_products')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('balance_cents')
