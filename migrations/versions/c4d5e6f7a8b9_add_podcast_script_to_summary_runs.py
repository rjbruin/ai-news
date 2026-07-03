"""add podcast_script to summary_runs

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_script', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('podcast_script')
