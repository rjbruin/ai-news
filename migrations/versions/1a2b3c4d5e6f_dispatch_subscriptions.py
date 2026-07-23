"""dispatch subscriptions: rename featured_summary_id, add is_system_dispatch

Introduces "Dispatch" subscriptions — any user can read another user's
Summary read-only, starting with a default "System Dispatch" every new
user is subscribed to. `users.featured_summary_id` already meant "the one
Summary driving my dashboard"; renamed to `subscribed_summary_id` since it
can now point at a Summary the user doesn't own. `summaries.is_system_dispatch`
marks the one Summary that's the default for new users — seeded here onto
the first configured admin account's (by ADMIN_EMAILS, lowest user id) most
recently created agentic_page Summary, if one exists. This is deployment-
specific (ADMIN_EMAILS varies per install), so it's looked up via the app's
own config rather than a hardcoded username.

Revision ID: 1a2b3c4d5e6f
Revises: 7ba395c379a9
Create Date: 2026-07-20 00:00:00.000000

"""
from alembic import op
from flask import current_app
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e6f'
down_revision = '7ba395c379a9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column(
            'featured_summary_id', new_column_name='subscribed_summary_id',
            existing_type=sa.Integer(),
        )
    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_system_dispatch', sa.Boolean(), nullable=False, server_default='0')
        )

    # Seed the system dispatch onto the first configured admin account's
    # most recently created agentic_page Summary, if one exists. No-op on a
    # fresh install (no admin emails configured yet, or that admin has no
    # Summary yet) — an admin can set it via the admin toggle once one does.
    admin_emails = [e.lower() for e in current_app.config.get("ADMIN_EMAILS", [])]
    if admin_emails:
        bind = op.get_bind()
        placeholders = ", ".join(f":email{i}" for i in range(len(admin_emails)))
        params = {f"email{i}": email for i, email in enumerate(admin_emails)}
        bind.execute(sa.text(f"""
            UPDATE summaries SET is_system_dispatch = 1
            WHERE id = (
                SELECT s.id FROM summaries s
                JOIN users u ON u.id = s.user_id
                WHERE lower(u.email) IN ({placeholders}) AND s.type_key = 'agentic_page'
                ORDER BY u.id ASC, s.created_at DESC
                LIMIT 1
            )
        """), params)


def downgrade():
    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.drop_column('is_system_dispatch')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column(
            'subscribed_summary_id', new_column_name='featured_summary_id',
            existing_type=sa.Integer(),
        )
