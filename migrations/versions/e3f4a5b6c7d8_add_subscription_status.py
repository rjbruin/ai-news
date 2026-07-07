"""add subscription_status to sources (newsletter confirmation flow)

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subscription_status', sa.String(length=20), nullable=True))

    # Every newsletter subscription that already exists was detected from mail
    # already flowing through the mailbox — there's no pending confirmation to
    # track, so they're all already-subscribed.
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE sources SET subscription_status = 'subscribed' WHERE parent_source_id IS NOT NULL"
    ))


def downgrade():
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_column('subscription_status')
