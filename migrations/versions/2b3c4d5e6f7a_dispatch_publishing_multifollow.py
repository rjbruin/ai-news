"""dispatch publishing + multi-follow

Turns Dispatch subscriptions into a many-to-many (a user follows any number of
published Dispatches) and adds publishing. Replaces the single
`users.subscribed_summary_id` FK with a `dispatch_subscriptions` association
table, carrying existing follows over. Adds `summaries.is_published` +
`summaries.published_name`, and publishes the current System Dispatch as
"AI Tech Dispatch" (keyed off is_system_dispatch, so deployment-agnostic).

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-07-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2b3c4d5e6f7a'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dispatch_subscriptions',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('summary_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['summary_id'], ['summaries.id']),
        sa.PrimaryKeyConstraint('user_id', 'summary_id'),
    )
    # Carry each user's single existing follow into the new table.
    op.execute("""
        INSERT INTO dispatch_subscriptions (user_id, summary_id)
        SELECT id, subscribed_summary_id FROM users
        WHERE subscribed_summary_id IS NOT NULL
    """)
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('subscribed_summary_id')

    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_published', sa.Boolean(), nullable=False, server_default='0')
        )
        batch_op.add_column(sa.Column('published_name', sa.String(length=25), nullable=True))
        batch_op.create_unique_constraint('uq_summaries_published_name', ['published_name'])

    # Publish the current System Dispatch as "AI Tech Dispatch".
    op.execute("""
        UPDATE summaries SET is_published = 1, published_name = 'AI Tech Dispatch'
        WHERE is_system_dispatch = 1
    """)


def downgrade():
    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.drop_constraint('uq_summaries_published_name', type_='unique')
        batch_op.drop_column('published_name')
        batch_op.drop_column('is_published')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subscribed_summary_id', sa.Integer(), nullable=True))

    # Best-effort restore: pick one followed dispatch per user (lowest summary id).
    op.execute("""
        UPDATE users SET subscribed_summary_id = (
            SELECT MIN(summary_id) FROM dispatch_subscriptions
            WHERE dispatch_subscriptions.user_id = users.id
        )
    """)
    op.drop_table('dispatch_subscriptions')
