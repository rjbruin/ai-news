"""add edition_recipients table

Lets a user manage multiple email addresses that receive their edition
emails (previously always just the account's own email). New addresses
need to click a confirmation link before mail is sent to them.

Revision ID: d8e9f0a1b2c3
Revises: b6c7d8e9f0a1
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8e9f0a1b2c3'
down_revision = 'b6c7d8e9f0a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'edition_recipients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('confirm_token', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'email', name='uq_edition_recipient'),
    )
    with op.batch_alter_table('edition_recipients', schema=None) as batch_op:
        batch_op.create_index('ix_edition_recipients_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_edition_recipients_confirm_token', ['confirm_token'], unique=True)

    # Backfill: every existing user's own email becomes their first, already-
    # confirmed recipient, so edition-email delivery for anyone who already
    # had "send as email newsletter" on doesn't silently stop until they
    # happen to open Settings.
    import datetime

    conn = op.get_bind()
    now = datetime.datetime.utcnow()
    users = conn.execute(sa.text("SELECT id, email FROM users")).fetchall()
    edition_recipients = sa.table(
        'edition_recipients',
        sa.column('user_id', sa.Integer()),
        sa.column('email', sa.String()),
        sa.column('confirmed_at', sa.DateTime()),
        sa.column('created_at', sa.DateTime()),
    )
    if users:
        conn.execute(
            edition_recipients.insert(),
            [
                {"user_id": u.id, "email": u.email, "confirmed_at": now, "created_at": now}
                for u in users
            ],
        )


def downgrade():
    with op.batch_alter_table('edition_recipients', schema=None) as batch_op:
        batch_op.drop_index('ix_edition_recipients_confirm_token')
        batch_op.drop_index('ix_edition_recipients_user_id')
    op.drop_table('edition_recipients')
