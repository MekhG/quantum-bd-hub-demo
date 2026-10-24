"""Week 6: procurement & quotes — price list, bundles, quotations (Word, from the team's template), procurement
checklists, app settings.

Buying routes, funding sources, checklist rules and quotation defaults are configuration data, added by
`python -m app.seed.seed_config`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20 02:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['app_settings', 'procurement_rules', 'price_items', 'bundles', 'bundle_items', 'procurement_items', 'quotes', 'quote_lines']


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings'))
    )
    op.create_table('procurement_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('buying_route', sa.String(length=80), nullable=False),
    sa.Column('item', sa.String(length=200), nullable=False),
    sa.Column('only_if_reseller', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_procurement_rules'))
    )
    op.create_index(op.f('ix_procurement_rules_buying_route'), 'procurement_rules', ['buying_route'], unique=False)
    op.create_table('price_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('unit', sa.String(length=40), server_default='unit', nullable=False),
    sa.Column('partner_price_inr', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('institute_price_inr', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('gst_pct', sa.Numeric(precision=5, scale=2), server_default='18', nullable=False),
    sa.Column('hsn_sac', sa.String(length=20), nullable=True),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_price_items_product_id_products'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_items'))
    )
    op.create_table('bundles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('phase', sa.String(length=40), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bundles')),
    sa.UniqueConstraint('name', name=op.f('uq_bundles_name'))
    )
    op.create_table('bundle_items',
    sa.Column('bundle_id', sa.Integer(), nullable=False),
    sa.Column('price_item_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['bundle_id'], ['bundles.id'], name=op.f('fk_bundle_items_bundle_id_bundles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['price_item_id'], ['price_items.id'], name=op.f('fk_bundle_items_price_item_id_price_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('bundle_id', 'price_item_id', name=op.f('pk_bundle_items'))
    )
    op.create_table('procurement_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('opportunity_id', sa.Integer(), nullable=False),
    sa.Column('item', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='to_do', nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=True),
    sa.Column('due_on', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('to_do', 'requested', 'received', 'submitted', 'not_needed')", name=op.f('ck_procurement_items_status_valid')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_procurement_items_document_id_documents'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], name=op.f('fk_procurement_items_opportunity_id_opportunities'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_procurement_items'))
    )
    op.create_index(op.f('ix_procurement_items_opportunity_id'), 'procurement_items', ['opportunity_id'], unique=False)
    op.create_table('quotes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('opportunity_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=40), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('status', sa.String(length=12), server_default='draft', nullable=False),
    sa.Column('price_basis', sa.String(length=10), server_default='institute', nullable=False),
    sa.Column('quote_date', sa.Date(), nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=False),
    sa.Column('discount_pct', sa.Numeric(precision=5, scale=2), server_default='0', nullable=False),
    sa.Column('subtotal_inr', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('discount_inr', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('gst_inr', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('total_inr', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('to_name', sa.String(length=160), nullable=True),
    sa.Column('to_designation', sa.String(length=160), nullable=True),
    sa.Column('to_institution', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('attention', sa.String(length=200), nullable=True),
    sa.Column('greeting', sa.String(length=200), nullable=True),
    sa.Column('prepared_by', sa.String(length=200), nullable=True),
    sa.Column('terms', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('drive_url', sa.String(length=1000), nullable=True),
    sa.Column('sent_on', sa.Date(), nullable=True),
    sa.Column('accepted_on', sa.Date(), nullable=True),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("price_basis IN ('institute', 'partner')", name=op.f('ck_quotes_basis_valid')),
    sa.CheckConstraint("status IN ('draft', 'sent', 'accepted', 'expired', 'superseded', 'cancelled')", name=op.f('ck_quotes_status_valid')),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_quotes_contact_id_contacts'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_quotes_created_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], name=op.f('fk_quotes_opportunity_id_opportunities'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quotes')),
    sa.UniqueConstraint('number', 'version', name='uq_quotes_number_version')
    )
    op.create_index(op.f('ix_quotes_opportunity_id'), 'quotes', ['opportunity_id'], unique=False)
    op.create_table('quote_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('quote_id', sa.Integer(), nullable=False),
    sa.Column('price_item_id', sa.Integer(), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('description', sa.String(length=400), nullable=False),
    sa.Column('details', sa.Text(), nullable=True),
    sa.Column('section', sa.String(length=20), server_default='other', nullable=False),
    sa.Column('unit', sa.String(length=40), nullable=True),
    sa.Column('hsn_sac', sa.String(length=20), nullable=True),
    sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
    sa.Column('unit_price_inr', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('gst_pct', sa.Numeric(precision=5, scale=2), server_default='18', nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['price_item_id'], ['price_items.id'], name=op.f('fk_quote_lines_price_item_id_price_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_quote_lines_product_id_products'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['quote_id'], ['quotes.id'], name=op.f('fk_quote_lines_quote_id_quotes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quote_lines'))
    )
    op.create_index(op.f('ix_quote_lines_quote_id'), 'quote_lines', ['quote_id'], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['app_settings', 'procurement_rules', 'price_items', 'bundles', 'bundle_items', 'procurement_items', 'quotes', 'quote_lines']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
