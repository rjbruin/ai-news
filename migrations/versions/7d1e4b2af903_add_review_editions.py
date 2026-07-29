"""add review editions: summary_runs.kind + summaries.review_period

Review editions are a slower retrospective cadence over a Dispatch's own
editions (see docs/review-editions-spec.md). They are stored as SummaryRuns on
the *same* Summary so they inherit its followers, publishing state, email
subscribers and podcast feed — hence a discriminator column rather than a
second Summary row.

Existing runs are all regular editions, which the server_default covers.

Revision ID: 7d1e4b2af903
Revises: bc1d1d9474fc
Create Date: 2026-07-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7d1e4b2af903'
down_revision = 'bc1d1d9474fc'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'kind', sa.String(length=16), nullable=False, server_default='edition',
        ))
        batch_op.create_index(
            'ix_summary_runs_summary_kind', ['summary_id', 'kind'], unique=False,
        )

    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('review_period', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('summaries', schema=None) as batch_op:
        batch_op.drop_column('review_period')

    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_summary_runs_summary_kind')
        batch_op.drop_column('kind')
