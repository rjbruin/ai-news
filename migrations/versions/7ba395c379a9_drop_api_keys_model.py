"""drop api_keys.model

Per-key model overrides are removed — the model for editions is set once
on the Settings page (summary.params["model"], see the earlier
"decouple edition model from API keys" change); source ingestion/tagging
now always uses the system default OPENROUTER_MODEL. Keeping a per-key
override around after that decoupling was confusing (it looked like it
still did something for editions, and nothing let a user notice it quietly
governed only ingestion).

Revision ID: 7ba395c379a9
Revises: b8dfc16f8f94
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7ba395c379a9'
down_revision = 'b8dfc16f8f94'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_column('model')


def downgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', sa.String(length=120), nullable=True))
