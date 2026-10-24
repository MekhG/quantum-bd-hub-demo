"""Slice 3: cadence engine fields, task assignments, and the real team.

- tracks: auto_start_cadence (start by itself once a contact is verified), daily_start_cap (optional limit).
- leads: cadence_status / started_on / due_on / ended_on, and stage_changed_at ("Ghosted on …").
- task_assignments: who gets each task type per track (set by the admin).
- users: real emails for two members (replacing placeholders) and a new member.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19 12:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLACEHOLDER_EMAILS = {
    "kabir@placeholder.invalid": "kabir@demo.example",
    "arjun@placeholder.invalid": "arjun@demo.example",
}
NEW_MEMBERS = (("Neha Iyer", "neha@demo.example", "member"),)

users = sa.table(
    "users",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("email", sa.String),
    sa.column("role", sa.String),
    sa.column("is_active", sa.Boolean),
)


def upgrade() -> None:
    op.add_column("tracks", sa.Column("auto_start_cadence", sa.Boolean(), server_default=sa.text("false"),
                                      nullable=False))
    op.add_column("tracks", sa.Column("daily_start_cap", sa.Integer(), nullable=True))
    op.add_column("leads", sa.Column("cadence_status", sa.String(length=20), nullable=True))
    op.add_column("leads", sa.Column("cadence_started_on", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("cadence_due_on", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("cadence_ended_on", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_leads_cadence_status"), "leads", ["cadence_status"], unique=False)

    op.create_table(
        "task_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("activity_type_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["activity_type_id"], ["activity_types.id"],
                                name=op.f("fk_task_assignments_activity_type_id_activity_types"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"],
                                name=op.f("fk_task_assignments_track_id_tracks"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_task_assignments_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_assignments")),
        sa.UniqueConstraint("track_id", "activity_type_id", name="uq_task_assignments_track_type"),
    )
    if op.get_bind().dialect.name == "postgresql":
        # Same protection as every other table: Supabase's public API can't read it.
        op.execute('ALTER TABLE "task_assignments" ENABLE ROW LEVEL SECURITY')

    bind = op.get_bind()
    if bind.execute(sa.select(sa.func.count()).select_from(users)).scalar():
        for old, new in PLACEHOLDER_EMAILS.items():
            taken = bind.execute(sa.select(users.c.id).where(sa.func.lower(users.c.email) == new)).scalar()
            if not taken:
                bind.execute(users.update().where(users.c.email == old).values(email=new))
        for name, email, role in NEW_MEMBERS:
            exists = bind.execute(sa.select(users.c.id).where(sa.func.lower(users.c.email) == email)).scalar()
            if not exists:
                bind.execute(users.insert().values(name=name, email=email, role=role, is_active=True))


def downgrade() -> None:
    op.drop_table("task_assignments")
    op.drop_index(op.f("ix_leads_cadence_status"), table_name="leads")
    for column in ("stage_changed_at", "cadence_ended_on", "cadence_due_on", "cadence_started_on", "cadence_status"):
        op.drop_column("leads", column)
    op.drop_column("tracks", "daily_start_cap")
    op.drop_column("tracks", "auto_start_cadence")
    # Team emails are left as they are: they're real people, not schema.
