"""add podcast_feed_token to users

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_feed_token', sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_users_podcast_feed_token'),
            ['podcast_feed_token'], unique=True,
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_podcast_feed_token'))
        batch_op.drop_column('podcast_feed_token')
