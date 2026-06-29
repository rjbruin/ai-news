"""add document, revision, parent_run_id to summary_runs

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-29 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('document', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('parent_run_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_summary_runs_parent_run_id', ['parent_run_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_summary_runs_parent_run_id', 'summary_runs', ['parent_run_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_summary_runs_parent_run_id', type_='foreignkey')
        batch_op.drop_index('ix_summary_runs_parent_run_id')
        batch_op.drop_column('parent_run_id')
        batch_op.drop_column('revision')
        batch_op.drop_column('document')
