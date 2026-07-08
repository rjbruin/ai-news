"""add users.has_seen_onboarding

Tracks whether the first-visit onboarding tutorial has been shown, so it
only ever appears once per account.

Revision ID: 117aa3fc3b06
Revises: d8e9f0a1b2c3
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '117aa3fc3b06'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('has_seen_onboarding', sa.Boolean(), nullable=False, server_default='0')
        )
    # Existing accounts already know their way around — only genuinely new
    # registrations (server_default False above) should see the tutorial.
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE users SET has_seen_onboarding = 1"))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('has_seen_onboarding')
