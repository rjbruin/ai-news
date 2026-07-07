"""global podcast settings + per-user podcast access flag

Adds users.podcast_enabled (admins always have access regardless) and the
admin_settings singleton table for the shared ElevenLabs voice/model
configuration. Migrates any existing per-user ElevenLabs voice/model values
into that shared row (best effort — picks the first user who had one
configured), then drops the old per-user ElevenLabs columns, since the
ElevenLabs API key itself is now a single global credential
(ELEVENLABS_API_KEY env var) rather than a per-user secret.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6c7d8e9f0a1'
down_revision = 'a5b6c7d8e9f0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('podcast_enabled', sa.Boolean(), nullable=False, server_default='0')
        )

    op.create_table(
        'admin_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('elevenlabs_voice_host_a', sa.String(length=120), nullable=True),
        sa.Column('elevenlabs_voice_host_b', sa.String(length=120), nullable=True),
        sa.Column('elevenlabs_model', sa.String(length=120), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    conn = op.get_bind()
    existing = conn.execute(sa.text(
        "SELECT elevenlabs_voice_host_a, elevenlabs_voice_host_b, elevenlabs_model FROM users "
        "WHERE elevenlabs_voice_host_a IS NOT NULL OR elevenlabs_voice_host_b IS NOT NULL "
        "OR elevenlabs_model IS NOT NULL LIMIT 1"
    )).fetchone()
    conn.execute(
        sa.text(
            "INSERT INTO admin_settings (elevenlabs_voice_host_a, elevenlabs_voice_host_b, elevenlabs_model) "
            "VALUES (:a, :b, :m)"
        ),
        {
            "a": existing[0] if existing else None,
            "b": existing[1] if existing else None,
            "m": existing[2] if existing else None,
        },
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('elevenlabs_api_key_enc')
        batch_op.drop_column('elevenlabs_voice_host_a')
        batch_op.drop_column('elevenlabs_voice_host_b')
        batch_op.drop_column('elevenlabs_model')


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('elevenlabs_api_key_enc', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('elevenlabs_voice_host_a', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('elevenlabs_voice_host_b', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('elevenlabs_model', sa.String(length=120), nullable=True))

    op.drop_table('admin_settings')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('podcast_enabled')
