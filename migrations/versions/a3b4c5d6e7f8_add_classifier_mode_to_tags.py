"""add classifier_mode override to tags

Lets an admin pin a topic's classification method (llm_only/hybrid/
classifier_only) instead of relying purely on the automatic label-count
graduation in app/tagging/engine.py. NULL (the default) keeps the existing
automatic behaviour.

Revision ID: a3b4c5d6e7f8
Revises: efe315d69bdc
Create Date: 2026-07-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3b4c5d6e7f8'
down_revision = 'efe315d69bdc'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.add_column(sa.Column('classifier_mode', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.drop_column('classifier_mode')
