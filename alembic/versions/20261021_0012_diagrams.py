"""Week 10: GTM charts — diagrams saved from the diagram builder (stored as their text outline).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 01:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['diagrams']


def upgrade() -> None:
    op.create_table('diagrams',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='custom', nullable=False),
    sa.Column('outline', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_diagrams_created_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id'], name=op.f('fk_diagrams_updated_by_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_diagrams'))
    )
    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['diagrams']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
