"""add api_keys, api_key_usage, sources.api_key_id, users.approved

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-07-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = 'b9c0d1e2f3a4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('key_enc', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('is_global', sa.Boolean(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index('ix_api_keys_owner_user_id', ['owner_user_id'], unique=False)

    op.create_table(
        'api_key_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('api_key_id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('cost', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('api_key_usage', schema=None) as batch_op:
        batch_op.create_index('ix_api_key_usage_api_key_id', ['api_key_id'], unique=False)
        batch_op.create_index('ix_api_key_usage_source_id', ['source_id'], unique=False)

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_key_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_sources_api_key_id', 'api_keys', ['api_key_id'], ['id']
        )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('approved', sa.Boolean(), nullable=False, server_default='0')
        )

    # Seed the global key row (its secret lives in OPENROUTER_API_KEY, not this
    # table) and point every pre-existing source at it, so ingestion keeps
    # working exactly as before for anyone upgrading.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "INSERT INTO api_keys (owner_user_id, label, provider, is_global, created_at) "
            "VALUES (NULL, 'Global OpenRouter key (shared by admins)', 'openrouter', 1, "
            "CURRENT_TIMESTAMP)"
        )
    )
    global_key_id = result.lastrowid
    if not global_key_id:
        # Some backends (e.g. Postgres) don't populate lastrowid; fall back to a lookup.
        global_key_id = conn.execute(
            sa.text("SELECT id FROM api_keys WHERE is_global = 1")
        ).scalar()
    conn.execute(
        sa.text("UPDATE sources SET api_key_id = :kid WHERE api_key_id IS NULL"),
        {"kid": global_key_id},
    )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('approved')

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sources_api_key_id', type_='foreignkey')
        batch_op.drop_column('api_key_id')

    with op.batch_alter_table('api_key_usage', schema=None) as batch_op:
        batch_op.drop_index('ix_api_key_usage_source_id')
        batch_op.drop_index('ix_api_key_usage_api_key_id')
    op.drop_table('api_key_usage')

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('ix_api_keys_owner_user_id')
    op.drop_table('api_keys')
