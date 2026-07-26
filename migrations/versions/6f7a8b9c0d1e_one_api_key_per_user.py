"""one OpenRouter API key per user

Simplifies from "N labeled ApiKey rows per user, each Source picks one, and
a separate users.edition_api_key_id selects which one funds editions" down
to exactly one personal ApiKey row per user, implicitly funding both their
own edition generation and every Source they own (ownerless/"system"
Sources stay funded by the global key, unchanged).

Data migration (lossy for anyone with 2+ personal keys — extras are merged
away, not reconstructable on downgrade): for each user with more than one
non-global ApiKey row, pick a canonical key (their old edition_api_key_id
row if set, else the most recently created row), repoint every
api_key_usage row from the other rows onto the canonical one (preserving
cost/usage history instead of losing it to cascade-delete), then delete the
now-redundant rows. sources.api_key_id and users.edition_api_key_id are then
dropped — funding is derived from Source.owner_user_id going forward — and a
partial unique index enforces at most one non-global key per owner.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-07-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f7a8b9c0d1e'
down_revision = '5e6f7a8b9c0d'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    users = conn.execute(sa.text(
        "SELECT id, edition_api_key_id FROM users"
    )).fetchall()

    for user_id, edition_api_key_id in users:
        rows = conn.execute(
            sa.text(
                "SELECT id FROM api_keys WHERE owner_user_id = :uid AND is_global = 0 "
                "ORDER BY created_at DESC"
            ),
            {"uid": user_id},
        ).fetchall()
        ids = [r[0] for r in rows]
        if len(ids) <= 1:
            continue

        if edition_api_key_id in ids:
            canonical = edition_api_key_id
        else:
            canonical = ids[0]  # most recently created (ORDER BY created_at DESC)
        duplicates = [i for i in ids if i != canonical]

        conn.execute(
            sa.text(
                "UPDATE api_key_usage SET api_key_id = :canonical "
                f"WHERE api_key_id IN ({','.join(str(d) for d in duplicates)})"
            ),
            {"canonical": canonical},
        )
        conn.execute(
            sa.text(
                f"DELETE FROM api_keys WHERE id IN ({','.join(str(d) for d in duplicates)})"
            ),
        )

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_column('api_key_id')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_edition_api_key_id', type_='foreignkey')
        batch_op.drop_column('edition_api_key_id')

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(
            'uq_api_keys_owner', ['owner_user_id'], unique=True,
            sqlite_where=sa.text('owner_user_id IS NOT NULL'),
        )


def downgrade():
    # Consolidated/merged key data is not reconstructable — this restores
    # the columns (empty) so the app's old code paths don't error, not the
    # original per-source/per-edition key assignments.
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('uq_api_keys_owner')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('edition_api_key_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_users_edition_api_key_id', 'api_keys', ['edition_api_key_id'], ['id']
        )

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_key_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_sources_api_key_id', 'api_keys', ['api_key_id'], ['id']
        )
