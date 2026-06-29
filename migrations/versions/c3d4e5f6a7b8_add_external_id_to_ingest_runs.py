"""add external_id to ingest_runs

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ingest_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_id', sa.String(length=500), nullable=True))
        batch_op.create_index('ix_ingest_runs_external_id', ['external_id'], unique=False)


def downgrade():
    with op.batch_alter_table('ingest_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_ingest_runs_external_id')
        batch_op.drop_column('external_id')
