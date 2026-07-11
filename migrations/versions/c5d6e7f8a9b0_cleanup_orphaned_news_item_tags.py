"""clean up orphaned news_item_tags rows

Deletes news_item_tags rows whose news_item_id no longer references any
existing news_items row. These accumulated because admin.source_reset()
bulk-deleted NewsItem rows via Query.delete(synchronize_session=False),
which bypasses the ORM-level cascade="all, delete-orphan" on
NewsItem.tag_links (that cascade only fires for db.session.delete() on an
individually loaded instance, not a bulk query delete) — fixed separately
in app/web/admin.py. This migration is a one-off data cleanup for rows
that already went stale before that fix landed.

Irreversible by design: the deleted rows have no NewsItem to reattach to,
so downgrade is a no-op.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-11 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "DELETE FROM news_item_tags WHERE news_item_id NOT IN (SELECT id FROM news_items)"
    )


def downgrade():
    pass
