"""add page_visits table

Anonymous, cookie-free page-visit counter for the admin usage-analytics
page — one row per (endpoint, day), incremented in place rather than logged
per hit, so it stays cheap regardless of traffic. See PageVisit.record().

Revision ID: 9a1b2c3d4e5f
Revises: 7d1e4b2af903
Create Date: 2026-07-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a1b2c3d4e5f'
down_revision = '7d1e4b2af903'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'page_visits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(length=120), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint', 'date', name='uq_page_visits_endpoint_date'),
    )


def downgrade():
    op.drop_table('page_visits')
