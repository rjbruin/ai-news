"""add invites table + admin_settings.registration_open

Registration is invite-only by default; admins can create invite links
with a configured number of uses, or flip registration_open to allow
self-service signup without an invite.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9f0a1b2c3d4'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('admin_settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('registration_open', sa.Boolean(), nullable=False, server_default='0')
        )

    op.create_table(
        'invites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('max_uses', sa.Integer(), nullable=False),
        sa.Column('uses_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
    with op.batch_alter_table('invites', schema=None) as batch_op:
        batch_op.create_index('ix_invites_code', ['code'], unique=True)


def downgrade():
    with op.batch_alter_table('invites', schema=None) as batch_op:
        batch_op.drop_index('ix_invites_code')
    op.drop_table('invites')

    with op.batch_alter_table('admin_settings', schema=None) as batch_op:
        batch_op.drop_column('registration_open')
