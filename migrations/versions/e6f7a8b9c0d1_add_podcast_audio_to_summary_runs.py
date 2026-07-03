"""add podcast_audio and news_podcast_audio to summary_runs

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_audio', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('news_podcast_audio', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('news_podcast_audio')
        batch_op.drop_column('podcast_audio')
