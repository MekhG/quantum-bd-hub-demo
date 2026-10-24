"""Slice 2.3: the team's revised pipeline stages, stage aliases, and a 'Stage change' activity type.

New order: New → First Contact Made → Replied to Queries → Sent Brochure → Online Demonstration →
Offline Demonstration → Sent Quotation → Deal PO Released (won) · Ghosted (nurture) · Rejected (lost).

- 'Physical Demonstration' is renamed 'Offline Demonstration'; 'Lost' is renamed 'Rejected'.
- 'Verified' and 'Negotiation' are removed: their leads move to New and Sent Quotation.
- stages.aliases lets old sheet wording ('Lost', 'Negotiation', 'Physical Demonstration') still import.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19 11:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

RENAMES = {"Physical Demonstration": "Offline Demonstration", "Lost": "Rejected"}
REMOVED = {"Verified": "New", "Negotiation": "Sent Quotation"}  # removed stage → where its leads go

# (name, outcome, aliases) in pipeline order — kept in step with app/seed/seed_config.py STAGES.
NEW_STAGES = (
    ("New", "open", ["verified", "fresh", "not contacted"]),
    ("First Contact Made", "open", ["first contact", "contacted", "email sent", "mail sent"]),
    ("Replied to Queries", "open", ["replied", "queries replied", "query replied"]),
    ("Sent Brochure", "open", ["brochure sent"]),
    ("Online Demonstration", "open", ["online demo", "online demo done", "virtual demo"]),
    ("Offline Demonstration", "open", ["physical demonstration", "physical demo", "offline demo",
                                       "in person demo", "on site demo"]),
    ("Sent Quotation", "open", ["quotation sent", "quote sent", "negotiation"]),
    ("Deal PO Released", "won", ["po released", "po received", "won", "client"]),
    ("Ghosted", "nurture", ["no response", "no reply"]),
    ("Rejected", "lost", ["lost", "not interested", "declined", "closed lost"]),
)

stages = sa.table(
    "stages",
    sa.column("id", sa.Integer),
    sa.column("name", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("outcome", sa.String),
    sa.column("is_active", sa.Boolean),
    sa.column("aliases", JSONType),
)
leads = sa.table("leads", sa.column("stage_id", sa.Integer))
activity_types = sa.table(
    "activity_types",
    sa.column("name", sa.String),
    sa.column("channel", sa.String),
    sa.column("default_duration_min", sa.Integer),
    sa.column("counts_as_touch", sa.Boolean),
    sa.column("sort_order", sa.Integer),
    sa.column("is_active", sa.Boolean),
)


def _stage_id(bind, name: str) -> int | None:
    return bind.execute(sa.select(stages.c.id).where(stages.c.name == name)).scalar()


def upgrade() -> None:
    op.add_column("stages", sa.Column("aliases", JSONType, nullable=True))
    bind = op.get_bind()

    # Only touch a fresh-from-seed database's stages: on an empty database the seed adds the new list itself.
    if bind.execute(sa.select(sa.func.count()).select_from(stages)).scalar():
        for old, new in RENAMES.items():
            if _stage_id(bind, old) is not None and _stage_id(bind, new) is None:
                bind.execute(stages.update().where(stages.c.name == old).values(name=new))
        for removed, target in REMOVED.items():
            removed_id, target_id = _stage_id(bind, removed), _stage_id(bind, target)
            if removed_id is None:
                continue
            if target_id is not None:
                bind.execute(leads.update().where(leads.c.stage_id == removed_id).values(stage_id=target_id))
                bind.execute(stages.delete().where(stages.c.id == removed_id))
            else:  # target missing (renamed by hand?) — keep the stage but hide it rather than lose leads
                bind.execute(stages.update().where(stages.c.id == removed_id).values(is_active=False))
        for order, (name, outcome, aliases) in enumerate(NEW_STAGES, start=1):
            if _stage_id(bind, name) is None:
                bind.execute(stages.insert().values(name=name, sort_order=order, outcome=outcome,
                                                    is_active=True, aliases=aliases))
            else:
                bind.execute(stages.update().where(stages.c.name == name)
                             .values(sort_order=order, outcome=outcome, aliases=aliases))

    if bind.execute(sa.select(sa.func.count()).select_from(activity_types)).scalar():
        exists = bind.execute(
            sa.select(sa.func.count()).select_from(activity_types).where(activity_types.c.name == "Stage change")
        ).scalar()
        if not exists:
            last = bind.execute(sa.select(sa.func.max(activity_types.c.sort_order))).scalar() or 0
            bind.execute(activity_types.insert().values(
                name="Stage change", channel="internal", default_duration_min=0, counts_as_touch=False,
                sort_order=last + 1, is_active=True,
            ))


def downgrade() -> None:
    bind = op.get_bind()
    for old, new in RENAMES.items():
        if _stage_id(bind, new) is not None and _stage_id(bind, old) is None:
            bind.execute(stages.update().where(stages.c.name == new).values(name=old))
    # Removed stages come back empty; leads that were moved stay where they are.
    old_order = ("New", "Verified", "First Contact Made", "Replied to Queries", "Sent Brochure",
                 "Online Demonstration", "Physical Demonstration", "Sent Quotation", "Negotiation",
                 "Deal PO Released", "Ghosted", "Lost")
    if bind.execute(sa.select(sa.func.count()).select_from(stages)).scalar():
        for order, name in enumerate(old_order, start=1):
            if _stage_id(bind, name) is None:
                bind.execute(stages.insert().values(name=name, sort_order=order, outcome="open", is_active=True))
            else:
                bind.execute(stages.update().where(stages.c.name == name).values(sort_order=order))
    op.drop_column("stages", "aliases")
