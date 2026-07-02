"""add share_token to summary_runs

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('summary_runs', sa.Column('share_token', sa.String(length=64), nullable=True))
    op.create_index('ix_summary_runs_share_token', 'summary_runs', ['share_token'], unique=True)


def downgrade():
    op.drop_index('ix_summary_runs_share_token', table_name='summary_runs')
    op.drop_column('summary_runs', 'share_token')
