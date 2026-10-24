"""Week 8: weekly report — weekly targets per person, each person's notes for the week, saved reports.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['report_notes', 'weekly_reports', 'weekly_targets']


def upgrade() -> None:
    op.create_table('report_notes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_report_notes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_notes')),
    sa.UniqueConstraint('week_start', 'user_id', name='uq_report_notes_week_user')
    )
    op.create_index(op.f('ix_report_notes_week_start'), 'report_notes', ['week_start'], unique=False)
    op.create_table('weekly_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='draft', nullable=False),
    sa.Column('highlights', sa.Text(), nullable=True),
    sa.Column('risks', sa.Text(), nullable=True),
    sa.Column('asks', sa.Text(), nullable=True),
    sa.Column('snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('finalised_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finalised_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'final')", name=op.f('ck_weekly_reports_status_valid')),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_weekly_reports_created_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['finalised_by_user_id'], ['users.id'], name=op.f('fk_weekly_reports_finalised_by_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weekly_reports')),
    sa.UniqueConstraint('week_start', name=op.f('uq_weekly_reports_week_start'))
    )
    op.create_table('weekly_targets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('metric', sa.String(length=40), nullable=False),
    sa.Column('target', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_weekly_targets_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weekly_targets')),
    sa.UniqueConstraint('week_start', 'user_id', 'metric', name='uq_weekly_targets_week_user_metric')
    )
    op.create_index(op.f('ix_weekly_targets_week_start'), 'weekly_targets', ['week_start'], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['report_notes', 'weekly_reports', 'weekly_targets']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
