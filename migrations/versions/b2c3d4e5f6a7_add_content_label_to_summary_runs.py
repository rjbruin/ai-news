"""add content and label to summary_runs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-22 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('label', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('content', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('content')
        batch_op.drop_column('label')
