"""dispatch description/pdf export, per-dispatch email subscriptions

Adds Summary.description and Summary.pdf_export_enabled; replaces the
per-user multi-address edition_recipients model with a single
User.newsletter_email (any follower can then opt a followed Dispatch into
email via the new dispatch_email_subscriptions table, instead of only the
Dispatch owner broadcasting to their own address list).

Data migration policy (lossy, one-way — cannot be reconstructed on
downgrade): for each user, newsletter_email is set to their confirmed
edition_recipients row matching their account email if one exists, else
their sole confirmed edition_recipients row if they have exactly one, else
left NULL. Any user with 2+ confirmed addresses loses all but the kept one.
dispatch_email_subscriptions is backfilled with (user, their own Dispatch)
for every user who both ends up with a confirmed newsletter_email and had
send_email=true set on their own Dispatch's params.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-07-25 00:00:00.000000

"""
import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5e6f7a8b9c0d'
down_revision = '4d5e6f7a8b9c'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column(
            'pdf_export_enabled', sa.Boolean(), nullable=False, server_default='0'
        ))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('newsletter_email', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('newsletter_email_confirmed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(
            'newsletter_email_confirm_token', sa.String(length=64), nullable=True
        ))
        batch_op.create_index(
            'ix_users_newsletter_email_confirm_token', ['newsletter_email_confirm_token'],
            unique=True,
        )

    op.create_table(
        'dispatch_email_subscriptions',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['summary_id'], ['summaries.id'], ),
        sa.PrimaryKeyConstraint('user_id', 'summary_id'),
    )

    conn = op.get_bind()
    now = __import__("datetime").datetime.utcnow()

    users = conn.execute(sa.text("SELECT id, email FROM users")).fetchall()
    for user in users:
        confirmed = conn.execute(
            sa.text(
                "SELECT email FROM edition_recipients "
                "WHERE user_id = :uid AND confirmed_at IS NOT NULL"
            ),
            {"uid": user.id},
        ).fetchall()
        chosen = None
        if any(r.email == user.email for r in confirmed):
            chosen = user.email
        elif len(confirmed) == 1:
            chosen = confirmed[0].email
        if chosen:
            conn.execute(
                sa.text(
                    "UPDATE users SET newsletter_email = :email, "
                    "newsletter_email_confirmed_at = :now WHERE id = :uid"
                ),
                {"email": chosen, "now": now, "uid": user.id},
            )

    # Backfill dispatch_email_subscriptions: users who both kept a confirmed
    # address above and had send_email=true on their own Dispatch.
    own_dispatches = conn.execute(
        sa.text("SELECT id, user_id, params FROM summaries WHERE type_key = 'agentic_page'")
    ).fetchall()
    confirmed_user_ids = {
        r.id for r in conn.execute(
            sa.text("SELECT id FROM users WHERE newsletter_email_confirmed_at IS NOT NULL")
        ).fetchall()
    }
    rows = []
    for s in own_dispatches:
        if s.user_id not in confirmed_user_ids:
            continue
        try:
            params = json.loads(s.params) if s.params else {}
        except (TypeError, ValueError):
            params = {}
        if params.get("send_email"):
            rows.append({"user_id": s.user_id, "summary_id": s.id})
    if rows:
        dispatch_email_subscriptions = sa.table(
            'dispatch_email_subscriptions',
            sa.column('user_id', sa.Integer()),
            sa.column('summary_id', sa.Integer()),
        )
        conn.execute(dispatch_email_subscriptions.insert(), rows)

    with op.batch_alter_table('edition_recipients', schema=None) as batch_op:
        batch_op.drop_index('ix_edition_recipients_confirm_token')
        batch_op.drop_index('ix_edition_recipients_user_id')
    op.drop_table('edition_recipients')


def downgrade():
    # Data reconstruction isn't possible — edition_recipients comes back empty.
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

    op.drop_table('dispatch_email_subscriptions')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_newsletter_email_confirm_token')
        batch_op.drop_column('newsletter_email_confirm_token')
        batch_op.drop_column('newsletter_email_confirmed_at')
        batch_op.drop_column('newsletter_email')

    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.drop_column('pdf_export_enabled')
        batch_op.drop_column('description')
