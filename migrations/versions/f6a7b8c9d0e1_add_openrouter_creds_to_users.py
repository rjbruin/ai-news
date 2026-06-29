"""add per-user openrouter credentials

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-29 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('openrouter_api_key_enc', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('openrouter_model', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('openrouter_model')
        batch_op.drop_column('openrouter_api_key_enc')
