"""add ingest_runs table and ingest_run_id to news_items

Revision ID: a1b2c3d4e5f6
Revises: f0000a0ba4ee
Create Date: 2026-06-22 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import app.models


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'f0000a0ba4ee'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ingest_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.Column('subject', sa.String(length=500), nullable=True),
        sa.Column('sender', sa.String(length=255), nullable=True),
        sa.Column('raw_body', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    with op.batch_alter_table('news_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ingest_run_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_news_items_ingest_run_id', ['ingest_run_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_news_items_ingest_run_id',
            'ingest_runs', ['ingest_run_id'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('news_items', schema=None) as batch_op:
        batch_op.drop_constraint('fk_news_items_ingest_run_id', type_='foreignkey')
        batch_op.drop_index('ix_news_items_ingest_run_id')
        batch_op.drop_column('ingest_run_id')

    op.drop_table('ingest_runs')
