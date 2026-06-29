"""add agent_memory table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-29 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'agent_memory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('edition_ts', sa.DateTime(), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['summary_id'], ['summaries.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('agent_memory', schema=None) as batch_op:
        batch_op.create_index('ix_agent_memory_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_agent_memory_summary_id', ['summary_id'], unique=False)
        batch_op.create_index('ix_agent_memory_lookup', ['user_id', 'summary_id', 'kind'], unique=False)


def downgrade():
    with op.batch_alter_table('agent_memory', schema=None) as batch_op:
        batch_op.drop_index('ix_agent_memory_lookup')
        batch_op.drop_index('ix_agent_memory_summary_id')
        batch_op.drop_index('ix_agent_memory_user_id')
    op.drop_table('agent_memory')
