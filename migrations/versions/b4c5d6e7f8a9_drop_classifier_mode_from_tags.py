"""drop classifier_mode override from tags

The per-topic manual classifier-mode override is being removed — every
topic now uses the same automatic LLM-first-then-classifier graduation,
with no way (or need) to pin it, and the distinction is no longer shown
to users at all (not even admins).

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.drop_column('classifier_mode')


def downgrade():
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.add_column(sa.Column('classifier_mode', sa.String(length=20), nullable=True))
