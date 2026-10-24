"""Week 7: news & signals — news searches and feeds, news items, signal types, signals saved against institutions,
and other names (aliases) for institutions so they can be spotted in headlines.

Signal types and the default news searches are configuration data, added by `python -m app.seed.seed_config`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['news_sources', 'signal_types', 'news_items', 'org_signals']


def upgrade() -> None:
    op.create_table('news_sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='google_news', nullable=False),
    sa.Column('query', sa.String(length=300), nullable=True),
    sa.Column('url', sa.String(length=1000), nullable=True),
    sa.Column('topic', sa.String(length=80), nullable=False),
    sa.Column('must_include', sa.String(length=300), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_count', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('google_news', 'rss')", name=op.f('ck_news_sources_kind_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_news_sources'))
    )
    op.create_table('signal_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=True),
    sa.Column('weight', sa.Integer(), server_default='10', nullable=False),
    sa.Column('decay_months', sa.Integer(), server_default='12', nullable=False),
    sa.Column('keywords', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_signal_types')),
    sa.UniqueConstraint('code', name=op.f('uq_signal_types_code')),
    sa.UniqueConstraint('name', name=op.f('uq_signal_types_name'))
    )
    op.create_table('news_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(length=1000), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('publisher', sa.String(length=200), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('topic', sa.String(length=80), nullable=True),
    sa.Column('suggested_type_id', sa.Integer(), nullable=True),
    sa.Column('matched_org_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='new', nullable=False),
    sa.Column('library_document_id', sa.Integer(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actioned_by_user_id', sa.Integer(), nullable=True),
    sa.Column('actioned_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('new', 'saved', 'signal', 'dismissed')", name=op.f('ck_news_items_status_valid')),
    sa.ForeignKeyConstraint(['actioned_by_user_id'], ['users.id'], name=op.f('fk_news_items_actioned_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['library_document_id'], ['documents.id'], name=op.f('fk_news_items_library_document_id_documents'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_id'], ['news_sources.id'], name=op.f('fk_news_items_source_id_news_sources'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['suggested_type_id'], ['signal_types.id'], name=op.f('fk_news_items_suggested_type_id_signal_types'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_news_items')),
    sa.UniqueConstraint('url', name=op.f('uq_news_items_url'))
    )
    op.create_index('ix_news_items_status_published', 'news_items', ['status', 'published_at'], unique=False)
    op.create_table('org_signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.Integer(), nullable=False),
    sa.Column('signal_type_id', sa.Integer(), nullable=False),
    sa.Column('observed_on', sa.Date(), nullable=False),
    sa.Column('source_url', sa.String(length=1000), nullable=True),
    sa.Column('news_item_id', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_org_signals_created_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['news_item_id'], ['news_items.id'], name=op.f('fk_org_signals_news_item_id_news_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organisations.id'], name=op.f('fk_org_signals_org_id_organisations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['signal_type_id'], ['signal_types.id'], name=op.f('fk_org_signals_signal_type_id_signal_types'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_org_signals'))
    )
    op.create_index(op.f('ix_org_signals_org_id'), 'org_signals', ['org_id'], unique=False)
    op.add_column('organisations', sa.Column('aliases', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))


    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['news_sources', 'signal_types', 'news_items', 'org_signals']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_column("organisations", "aliases")
    for table in reversed(TABLES):
        op.drop_table(table)
