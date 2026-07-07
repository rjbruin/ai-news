"""add parent_source_id to sources (newsletter subscription split)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_source_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_sources_parent_source_id', ['parent_source_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_sources_parent_source_id', 'sources', ['parent_source_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sources_parent_source_id', type_='foreignkey')
        batch_op.drop_index('ix_sources_parent_source_id')
        batch_op.drop_column('parent_source_id')
