"""Add ElevenLabs and podcast fields to users.

Revision ID: f7b8c9d0e1f2
Revises: e1f2a3b4c5d6
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa

revision = 'f7b8c9d0e1f2'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('elevenlabs_api_key_enc', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('elevenlabs_voice_host_a', sa.String(length=120), nullable=True))
    op.add_column('users', sa.Column('elevenlabs_voice_host_b', sa.String(length=120), nullable=True))
    op.add_column('users', sa.Column('elevenlabs_model', sa.String(length=120), nullable=True))
    op.add_column('users', sa.Column('podcast_auto_generate', sa.Boolean(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('users', 'podcast_auto_generate')
    op.drop_column('users', 'elevenlabs_model')
    op.drop_column('users', 'elevenlabs_voice_host_b')
    op.drop_column('users', 'elevenlabs_voice_host_a')
    op.drop_column('users', 'elevenlabs_api_key_enc')
