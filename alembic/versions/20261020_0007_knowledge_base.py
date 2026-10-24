"""Week 5: knowledge base — documents (links), scripts with fill-in fields, objection library.

Document types and the flagged-words list are configuration data (option_lists), added by
`python -m app.seed.seed_config`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 02:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ['documents', 'objections', 'scripts', 'document_products', 'document_tracks', 'objection_products', 'objection_tracks', 'activity_documents']


def upgrade() -> None:
    op.create_table('documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('doc_type', sa.String(length=60), nullable=False),
    sa.Column('url', sa.String(length=1000), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('key_text', sa.Text(), nullable=True),
    sa.Column('tags', sa.String(length=400), nullable=True),
    sa.Column('version_label', sa.String(length=40), nullable=True),
    sa.Column('owner_user_id', sa.Integer(), nullable=True),
    sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
    sa.Column('family_id', sa.Integer(), nullable=True),
    sa.Column('version_no', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='draft', nullable=False),
    sa.Column('approved_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'approved', 'archived')", name=op.f('ck_documents_status_valid')),
    sa.ForeignKeyConstraint(['approved_by_user_id'], ['users.id'], name=op.f('fk_documents_approved_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_documents_owner_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    op.create_index(op.f('ix_documents_family_id'), 'documents', ['family_id'], unique=False)
    op.create_table('objections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('objection', sa.Text(), nullable=False),
    sa.Column('response', sa.Text(), nullable=False),
    sa.Column('tags', sa.String(length=400), nullable=True),
    sa.Column('status', sa.String(length=10), server_default='draft', nullable=False),
    sa.Column('owner_user_id', sa.Integer(), nullable=True),
    sa.Column('times_used', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_pinned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'approved', 'archived')", name=op.f('ck_objections_status_valid')),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_objections_owner_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_objections'))
    )
    op.create_table('scripts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=10), server_default='email', nullable=False),
    sa.Column('track_id', sa.Integer(), nullable=True),
    sa.Column('cadence_step_id', sa.Integer(), nullable=True),
    sa.Column('subject', sa.String(length=300), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('owner_user_id', sa.Integer(), nullable=True),
    sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
    sa.Column('family_id', sa.Integer(), nullable=True),
    sa.Column('version_no', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='draft', nullable=False),
    sa.Column('approved_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('email', 'call', 'whatsapp', 'linkedin')", name=op.f('ck_scripts_kind_valid')),
    sa.CheckConstraint("status IN ('draft', 'approved', 'archived')", name=op.f('ck_scripts_status_valid')),
    sa.ForeignKeyConstraint(['approved_by_user_id'], ['users.id'], name=op.f('fk_scripts_approved_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['cadence_step_id'], ['cadence_steps.id'], name=op.f('fk_scripts_cadence_step_id_cadence_steps'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_scripts_owner_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], name=op.f('fk_scripts_track_id_tracks'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scripts'))
    )
    op.create_index(op.f('ix_scripts_family_id'), 'scripts', ['family_id'], unique=False)
    op.create_table('document_products',
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_products_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_document_products_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id', 'product_id', name=op.f('pk_document_products'))
    )
    op.create_table('document_tracks',
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('track_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_tracks_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], name=op.f('fk_document_tracks_track_id_tracks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id', 'track_id', name=op.f('pk_document_tracks'))
    )
    op.create_table('objection_products',
    sa.Column('objection_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['objection_id'], ['objections.id'], name=op.f('fk_objection_products_objection_id_objections'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_objection_products_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('objection_id', 'product_id', name=op.f('pk_objection_products'))
    )
    op.create_table('objection_tracks',
    sa.Column('objection_id', sa.Integer(), nullable=False),
    sa.Column('track_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['objection_id'], ['objections.id'], name=op.f('fk_objection_tracks_objection_id_objections'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], name=op.f('fk_objection_tracks_track_id_tracks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('objection_id', 'track_id', name=op.f('pk_objection_tracks'))
    )
    op.create_table('activity_documents',
    sa.Column('activity_id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['activity_id'], ['activities.id'], name=op.f('fk_activity_documents_activity_id_activities'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_activity_documents_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('activity_id', 'document_id', name=op.f('pk_activity_documents'))
    )
    # SQLite (tests) can't add a foreign key with ALTER TABLE, so use Alembic's batch mode (a no-op wrapper on Postgres).
    with op.batch_alter_table("activities") as batch:
        batch.add_column(sa.Column("script_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(op.f("fk_activities_script_id_scripts"), "scripts", ["script_id"], ["id"],
                                 ondelete="SET NULL")
    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read these.
        for table in ['documents', 'objections', 'scripts', 'document_products', 'document_tracks', 'objection_products', 'objection_tracks', 'activity_documents']:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    with op.batch_alter_table("activities") as batch:
        batch.drop_constraint(op.f("fk_activities_script_id_scripts"), type_="foreignkey")
        batch.drop_column("script_id")
    for table in reversed(TABLES):
        op.drop_table(table)
