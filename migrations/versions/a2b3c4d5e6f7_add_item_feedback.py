"""add item_feedback table

Per-reader up/down votes on individual item blocks within an edition, the
raw signal behind the topic-tier suggestions and the agent's per-item
reader_signal hint (see app/services/reader_feedback.py).

Anchored on (run_id, block_id) because that's what the reader actually saw;
item_id rides along where the block cites one, since that's the part that
generalises across editions.

Revision ID: a2b3c4d5e6f7
Revises: 9a1b2c3d4e5f
Create Date: 2026-08-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2b3c4d5e6f7'
down_revision = '9a1b2c3d4e5f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'item_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('block_id', sa.String(length=64), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.Column('vote', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['run_id'], ['summary_runs.id']),
        sa.ForeignKeyConstraint(['item_id'], ['news_items.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'run_id', 'block_id', name='uq_item_feedback_user_block'),
    )
    with op.batch_alter_table('item_feedback', schema=None) as batch_op:
        batch_op.create_index('ix_item_feedback_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_item_feedback_run_id', ['run_id'], unique=False)
        batch_op.create_index('ix_item_feedback_item_id', ['item_id'], unique=False)


def downgrade():
    with op.batch_alter_table('item_feedback', schema=None) as batch_op:
        batch_op.drop_index('ix_item_feedback_item_id')
        batch_op.drop_index('ix_item_feedback_run_id')
        batch_op.drop_index('ix_item_feedback_user_id')
    op.drop_table('item_feedback')
