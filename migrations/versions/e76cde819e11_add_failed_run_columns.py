"""add summary_runs.error_message, summary_runs.retry_context

Lets a failed generation/revision be persisted as a SummaryRun (status =
"failed") instead of leaving no trace — see
app.services.summarize._persist_failed_run and web.edition_retry.

Revision ID: e76cde819e11
Revises: ccd68373aa3f
Create Date: 2026-07-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e76cde819e11'
down_revision = 'ccd68373aa3f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('error_message', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('retry_context', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('retry_context')
        batch_op.drop_column('error_message')
