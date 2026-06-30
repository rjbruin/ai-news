"""add featured_summary_id to users

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-30 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('featured_summary_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_users_featured_summary_id', 'summaries', ['featured_summary_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_featured_summary_id', type_='foreignkey')
        batch_op.drop_column('featured_summary_id')
