"""unify edition and source API keys

Adds users.edition_api_key_id and migrates each user's old per-user
OpenRouter credentials (users.openrouter_api_key_enc / openrouter_model) into
a personal ApiKey row, selected as their edition key — then drops the old
columns, since editions and sources now share one API key system.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-07-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a5b6c7d8e9f0'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('edition_api_key_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_users_edition_api_key_id', 'api_keys', ['edition_api_key_id'], ['id']
        )

    conn = op.get_bind()
    users = conn.execute(sa.text(
        "SELECT id, openrouter_api_key_enc, openrouter_model FROM users "
        "WHERE openrouter_api_key_enc IS NOT NULL"
    )).fetchall()
    for user_id, key_enc, model in users:
        conn.execute(
            sa.text(
                "INSERT INTO api_keys "
                "(owner_user_id, label, provider, key_enc, model, is_global, created_at) "
                "VALUES (:owner, 'Edition key (migrated)', 'openrouter', :key_enc, :model, "
                "0, CURRENT_TIMESTAMP)"
            ),
            {"owner": user_id, "key_enc": key_enc, "model": model},
        )
        new_id = conn.execute(
            sa.text("SELECT id FROM api_keys WHERE owner_user_id = :owner ORDER BY id DESC LIMIT 1"),
            {"owner": user_id},
        ).scalar()
        conn.execute(
            sa.text("UPDATE users SET edition_api_key_id = :kid WHERE id = :uid"),
            {"kid": new_id, "uid": user_id},
        )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('openrouter_api_key_enc')
        batch_op.drop_column('openrouter_model')


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('openrouter_api_key_enc', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('openrouter_model', sa.String(length=120), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT u.id, k.key_enc, k.model FROM users u "
        "JOIN api_keys k ON k.id = u.edition_api_key_id"
    )).fetchall()
    for user_id, key_enc, model in rows:
        conn.execute(
            sa.text(
                "UPDATE users SET openrouter_api_key_enc = :key_enc, openrouter_model = :model "
                "WHERE id = :uid"
            ),
            {"key_enc": key_enc, "model": model, "uid": user_id},
        )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_edition_api_key_id', type_='foreignkey')
        batch_op.drop_column('edition_api_key_id')
