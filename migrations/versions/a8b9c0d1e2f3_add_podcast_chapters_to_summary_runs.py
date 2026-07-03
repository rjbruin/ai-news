"""add podcast_chapters and news_podcast_chapters to summary_runs

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'a8b9c0d1e2f3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_chapters', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('news_podcast_chapters', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('news_podcast_chapters')
        batch_op.drop_column('podcast_chapters')
