"""topics rewrite: tags.updated_at/archived_at, news_item_tags.user_id

Adds soft-delete (archived_at) and an audit updated_at to tags, and adds
news_item_tags.user_id so a private topic's classification can be scoped to
its owner without being visible to everyone. Backfills user_id for existing
private-topic (scope='user') NewsItemTag rows from their tag's owner_user_id
— without this, those historical rows would default to user_id=NULL (global)
post-migration and silently leak to every user.

Revision ID: efe315d69bdc
Revises: f02aa2e20e4f
Create Date: 2026-07-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'efe315d69bdc'
down_revision = 'f02aa2e20e4f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))

    op.execute("UPDATE tags SET updated_at = created_at WHERE updated_at IS NULL")

    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.alter_column('updated_at', nullable=False)

    with op.batch_alter_table('news_item_tags', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_news_item_tags_user_id', 'users', ['user_id'], ['id']
        )
        batch_op.drop_constraint('uq_item_tag', type_='unique')
        batch_op.create_unique_constraint(
            'uq_item_tag_user', ['news_item_id', 'tag_id', 'user_id']
        )

    op.create_index(
        'uq_item_tag_global', 'news_item_tags', ['news_item_id', 'tag_id'],
        unique=True, sqlite_where=sa.text('user_id IS NULL'),
    )

    # Backfill: pre-existing labels for private topics were previously
    # applied without any per-user scoping — retroactively scope them to
    # their tag's owner so they don't become visible globally.
    op.execute("""
        UPDATE news_item_tags
        SET user_id = (SELECT owner_user_id FROM tags WHERE tags.id = news_item_tags.tag_id)
        WHERE tag_id IN (SELECT id FROM tags WHERE scope = 'user')
    """)


def downgrade():
    op.drop_index('uq_item_tag_global', table_name='news_item_tags')
    with op.batch_alter_table('news_item_tags', schema=None) as batch_op:
        batch_op.drop_constraint('uq_item_tag_user', type_='unique')
        batch_op.drop_constraint('fk_news_item_tags_user_id', type_='foreignkey')
        batch_op.drop_column('user_id')
        batch_op.create_unique_constraint('uq_item_tag', ['news_item_id', 'tag_id'])

    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.drop_column('archived_at')
        batch_op.drop_column('updated_at')
