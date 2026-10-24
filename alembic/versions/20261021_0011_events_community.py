"""Week 9: events & community — events (hackathons, workshops…), community members (one per email, with consent
and an 18-or-older confirmation) and who took part in which event.

Event types, member levels and the 'Event participation' signal type are configuration data, added by
`python -m app.seed.seed_config`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21 01:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['events', 'community_members', 'event_participants']


def upgrade() -> None:
    op.create_table('community_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('phone', sa.String(length=40), nullable=True),
    sa.Column('education', sa.String(length=255), nullable=True),
    sa.Column('level', sa.String(length=40), nullable=True),
    sa.Column('institution', sa.String(length=255), nullable=True),
    sa.Column('org_id', sa.Integer(), nullable=True),
    sa.Column('city', sa.String(length=80), nullable=True),
    sa.Column('state', sa.String(length=60), nullable=True),
    sa.Column('interests', sa.Text(), nullable=True),
    sa.Column('github_url', sa.String(length=500), nullable=True),
    sa.Column('website_url', sa.String(length=500), nullable=True),
    sa.Column('linkedin_url', sa.String(length=500), nullable=True),
    sa.Column('project_links', sa.Text(), nullable=True),
    sa.Column('consent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('consent_on', sa.Date(), nullable=True),
    sa.Column('is_adult', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('source', sa.String(length=160), nullable=True),
    sa.Column('do_not_contact', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organisations.id'], name=op.f('fk_community_members_org_id_organisations'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_community_members'))
    )
    op.create_index(op.f('ix_community_members_org_id'), 'community_members', ['org_id'], unique=False)
    op.create_index('uq_community_members_email_lower', 'community_members', [sa.literal_column('lower(email)')], unique=True)
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('start_on', sa.Date(), nullable=False),
    sa.Column('end_on', sa.Date(), nullable=True),
    sa.Column('mode', sa.String(length=10), server_default='online', nullable=False),
    sa.Column('venue', sa.String(length=255), nullable=True),
    sa.Column('partner', sa.String(length=120), nullable=True),
    sa.Column('form_url', sa.String(length=1000), nullable=True),
    sa.Column('cost_inr', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='planned', nullable=False),
    sa.Column('owner_user_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('online', 'offline', 'hybrid')", name=op.f('ck_events_mode_valid')),
    sa.CheckConstraint("status IN ('planned', 'open', 'done', 'cancelled')", name=op.f('ck_events_status_valid')),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_events_owner_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    op.create_index(op.f('ix_events_start_on'), 'events', ['start_on'], unique=False)
    op.create_table('event_participants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('team_name', sa.String(length=160), nullable=True),
    sa.Column('role', sa.String(length=20), server_default='participant', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='registered', nullable=False),
    sa.Column('project_title', sa.String(length=255), nullable=True),
    sa.Column('project_link', sa.String(length=1000), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint("role IN ('participant', 'mentor', 'judge', 'speaker', 'volunteer', 'organiser')", name=op.f('ck_event_participants_role_valid')),
    sa.CheckConstraint("status IN ('registered', 'attended', 'no_show', 'finalist', 'winner')", name=op.f('ck_event_participants_status_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_participants_event_id_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['member_id'], ['community_members.id'], name=op.f('fk_event_participants_member_id_community_members'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_participants')),
    sa.UniqueConstraint('event_id', 'member_id', name='uq_event_participants_event_member')
    )
    op.create_index(op.f('ix_event_participants_event_id'), 'event_participants', ['event_id'], unique=False)
    op.create_index(op.f('ix_event_participants_member_id'), 'event_participants', ['member_id'], unique=False)


    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['events', 'community_members', 'event_participants']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
