"""allow one ApiKey per (owner, provider) instead of just per owner

Personal API keys now support more than one provider per user (OpenRouter
and ElevenLabs), so the "one key per user" unique index needs to become
"one key per user per provider" — otherwise a user could never have both.

Revision ID: bc1d1d9474fc
Revises: 6f7a8b9c0d1e
Create Date: 2026-07-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'bc1d1d9474fc'
down_revision = '6f7a8b9c0d1e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('uq_api_keys_owner')
        batch_op.create_index(
            'uq_api_keys_owner_provider', ['owner_user_id', 'provider'], unique=True,
            sqlite_where=sa.text('owner_user_id IS NOT NULL'),
        )


def downgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('uq_api_keys_owner_provider')
        batch_op.create_index(
            'uq_api_keys_owner', ['owner_user_id'], unique=True,
            sqlite_where=sa.text('owner_user_id IS NOT NULL'),
        )
