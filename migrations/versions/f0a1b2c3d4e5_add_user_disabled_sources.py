"""add user_disabled_sources table

Lets a user turn a (shared) source off for their own editions without
affecting anyone else — every source is on by default; this table only
tracks per-user exceptions.

Revision ID: f0a1b2c3d4e5
Revises: d8e9f0a1b2c3
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0a1b2c3d4e5'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_disabled_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'source_id', name='uq_user_disabled_source'),
    )
    with op.batch_alter_table('user_disabled_sources', schema=None) as batch_op:
        batch_op.create_index('ix_user_disabled_sources_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_user_disabled_sources_source_id', ['source_id'], unique=False)


def downgrade():
    with op.batch_alter_table('user_disabled_sources', schema=None) as batch_op:
        batch_op.drop_index('ix_user_disabled_sources_source_id')
        batch_op.drop_index('ix_user_disabled_sources_user_id')
    op.drop_table('user_disabled_sources')
