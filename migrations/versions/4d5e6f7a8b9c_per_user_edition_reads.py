"""per-user edition read status

A Dispatch can now be followed by multiple readers, so a single shared
`summary_runs.read_at` no longer makes sense — marking it read for one
reader would mark it read for everyone. Replaces it with an `edition_reads`
table (user_id, run_id, read_at). Existing read marks are backfilled onto
the Dispatch's owner (the only user who could previously toggle them).

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-07-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4d5e6f7a8b9c'
down_revision = '3c4d5e6f7a8b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'edition_reads',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['run_id'], ['summary_runs.id']),
        sa.PrimaryKeyConstraint('user_id', 'run_id'),
    )
    op.execute("""
        INSERT INTO edition_reads (user_id, run_id, read_at)
        SELECT s.user_id, sr.id, sr.read_at
        FROM summary_runs sr
        JOIN summaries s ON s.id = sr.summary_id
        WHERE sr.read_at IS NOT NULL
    """)
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.drop_column('read_at')


def downgrade():
    with op.batch_alter_table('summary_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('read_at', sa.DateTime(), nullable=True))
    op.execute("""
        UPDATE summary_runs SET read_at = (
            SELECT MIN(er.read_at) FROM edition_reads er
            WHERE er.run_id = summary_runs.id
        )
    """)
    op.drop_table('edition_reads')
