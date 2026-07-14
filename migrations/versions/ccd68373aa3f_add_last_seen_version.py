"""add users.last_seen_version

Tracks which release's changelog a user has last been shown, so the
post-update changelog modal only ever appears once per new version.

Revision ID: ccd68373aa3f
Revises: c5d6e7f8a9b0
Create Date: 2026-07-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ccd68373aa3f'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_seen_version', sa.String(length=32), nullable=True))
    # Back-date existing accounts to the version immediately prior to this
    # release (not the new one) so they see the changelog entry for this
    # release once, instead of being silently caught up on it.
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE users SET last_seen_version = '0.23.0'"))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('last_seen_version')
