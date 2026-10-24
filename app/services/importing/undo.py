"""Undo an import: delete the rows it created, unless other work now depends on them.

Updates the import made to existing rows (filled-in blanks, stages moved forward) are not reversed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    AuditLog,
    CommunityMember,
    EventParticipant,
    Contact,
    ImportBatch,
    ImportBatchItem,
    Lead,
    Opportunity,
    Organisation,
    User,
)

_CHUNK = 1000


@dataclass
class UndoResult:
    deleted: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[i : i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def undo_import(session: Session, batch_id: int, user: User | None) -> UndoResult:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ValueError("Import not found.")
    if batch.undone_at is not None:
        raise ValueError("This import was already undone.")

    ids: dict[str, list[int]] = defaultdict(list)
    for entity, entity_id in session.execute(
        select(ImportBatchItem.entity, ImportBatchItem.entity_id).where(ImportBatchItem.batch_id == batch_id)
    ):
        ids[entity].append(entity_id)

    result = UndoResult()
    # Order matters: children before parents.
    plan = [
        # Community imports (Week 9): participations first, then members who took part in nothing else.
        ("event_participants", EventParticipant, lambda chunk: [EventParticipant.id.in_(chunk)]),
        (
            "community_members",
            CommunityMember,
            lambda chunk: [
                CommunityMember.id.in_(chunk),
                ~exists().where(EventParticipant.member_id == CommunityMember.id),
            ],
        ),
        ("activities", Activity, lambda chunk: [Activity.id.in_(chunk)]),
        ("opportunities", Opportunity, lambda chunk: [Opportunity.id.in_(chunk)]),
        (
            "leads",
            Lead,
            lambda chunk: [
                Lead.id.in_(chunk),
                # Keep leads that now carry activities or deals from elsewhere.
                ~exists().where(Activity.lead_id == Lead.id),
                ~exists().where(Opportunity.lead_id == Lead.id),
            ],
        ),
        (
            "contacts",
            Contact,
            lambda chunk: [
                Contact.id.in_(chunk),
                ~exists().where(ActivityParticipant.contact_id == Contact.id),
            ],
        ),
        (
            "organisations",
            Organisation,
            lambda chunk: [
                Organisation.id.in_(chunk),
                ~exists().where(Contact.org_id == Organisation.id),
                ~exists().where(Lead.org_id == Organisation.id),
                ~exists().where(Activity.org_id == Organisation.id),
            ],
        ),
    ]
    for entity, model, conditions in plan:
        wanted = ids.get(entity, [])
        removed = 0
        for chunk in _chunks(wanted):
            outcome = session.execute(
                delete(model).where(*conditions(chunk)).execution_options(synchronize_session=False)
            )
            removed += outcome.rowcount or 0
        if wanted:
            result.deleted[entity] = removed
            result.kept[entity] = len(wanted) - removed

    batch.undone_at = datetime.now(timezone.utc)
    batch.undone_by_user_id = user.id if user else None
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            entity="import_batches",
            entity_id=str(batch_id),
            action="delete",
            diff={"undo": True, "deleted": result.deleted, "kept": result.kept},
        )
    )
    session.flush()
    return result
