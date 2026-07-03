"""add pdf_font_scale to users

Revision ID: b3c4d5e6f7a8
Revises: f7b8c9d0e1f2
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3c4d5e6f7a8'
down_revision = 'f7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'pdf_font_scale', sa.Integer(),
            nullable=False, server_default='80',
        ))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('pdf_font_scale')
