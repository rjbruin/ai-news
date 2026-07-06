"""add pdf_file to summary_runs

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'b9c0d1e2f3a4'
down_revision = 'a8b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pdf_file', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('pdf_file')
