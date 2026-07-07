"""add ignored_senders table

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-07-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a5b6c7d8e9'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ignored_senders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('mailbox_source_id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['mailbox_source_id'], ['sources.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mailbox_source_id', 'email', name='uq_ignored_sender'),
    )
    with op.batch_alter_table('ignored_senders', schema=None) as batch_op:
        batch_op.create_index('ix_ignored_senders_mailbox_source_id', ['mailbox_source_id'], unique=False)


def downgrade():
    with op.batch_alter_table('ignored_senders', schema=None) as batch_op:
        batch_op.drop_index('ix_ignored_senders_mailbox_source_id')
    op.drop_table('ignored_senders')
