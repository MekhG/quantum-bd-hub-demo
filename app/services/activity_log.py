"""Logging calls, WhatsApp, meetings, demos…, editing the timeline, and the data behind the Today page."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    AuditLog,
    CadenceStep,
    Contact,
    Document,
    Lead,
    Organisation,
    Stage,
    Track,
    User,
)
from app.services.cadence import IST, at_ist, complete_activity, ist_date, steps_per_track, today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
HIDDEN_TYPES = ("Stage change",)  # written by the app, not logged by people
INBOUND_TYPES = ("Website enquiry", "Other enquiry")


class LogError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


@dataclass
class NextStep:
    type_name: str
    day: date
    at: time | None = None
    owner_id: int | None = None
    title: str | None = None


@dataclass
class LogEntry:
    org_id: int
    type_name: str
    when: datetime
    lead_id: int | None = None
    direction: str = "outbound"
    status: str = "done"  # 'done' = it happened; 'planned' = book it for later (e.g. a demo next week)
    title: str | None = None
    notes: str | None = None
    outcome: str | None = None
    contact_ids: list[int] = field(default_factory=list)
    duration_min: int | None = None
    owner_id: int | None = None
    next_step: NextStep | None = None
    document_ids: list[int] = field(default_factory=list)  # Library documents shared in this activity
    script_id: int | None = None                           # script used, from the script library


def _kind(session: Session, name: str) -> ActivityType:
    kind = session.scalar(select(ActivityType).where(ActivityType.name == name))
    if kind is None or name in HIDDEN_TYPES:
        raise LogError(f"Unknown activity type '{name}'.")
    return kind


def _title(kind: ActivityType, contacts: list[Contact], org: Organisation) -> str:
    who = ", ".join(c.name for c in contacts[:2]) or org.name
    return f"{kind.name} — {who}"[:200]


def log_activity(session: Session, entry: LogEntry, user: User) -> tuple[Activity, list[str]]:
    """Saves one activity (and its next step). Returns it plus messages for the person."""
    _require(user)
    org = session.get(Organisation, entry.org_id)
    if org is None:
        raise LogError("Pick an organisation.")
    lead = session.get(Lead, entry.lead_id) if entry.lead_id else None
    if lead is not None and lead.org_id != org.id:
        raise LogError("That lead belongs to a different organisation.")
    if entry.direction not in ("outbound", "inbound", "internal"):
        raise LogError("Direction must be outbound, inbound or internal.")
    if entry.status not in ("done", "planned"):
        raise LogError("Status must be done or planned.")
    kind = _kind(session, entry.type_name)
    contacts = [c for c in (session.get(Contact, cid) for cid in entry.contact_ids) if c is not None]
    if any(c.org_id != org.id for c in contacts):
        raise LogError("A chosen contact belongs to a different organisation.")
    when = entry.when if entry.when.tzinfo else entry.when.replace(tzinfo=IST)
    messages: list[str] = []

    activity: Activity | None = None
    if entry.status == "done" and lead is not None and entry.direction == "outbound":
        # Doing a task the cadence planned ticks it off instead of creating a duplicate.
        activity = session.scalar(
            select(Activity).where(Activity.lead_id == lead.id, Activity.status == "planned",
                                   Activity.type_id == kind.id, Activity.cadence_step_id.is_not(None))
            .order_by(Activity.scheduled_at).limit(1)
        )
        if activity is not None:
            messages.append(f"Ticked off the planned task “{activity.title}”.")
            known = {p.contact_id for p in activity.participants}
            for contact in contacts:
                if contact.id not in known:
                    activity.participants.append(ActivityParticipant(contact=contact))
            if entry.duration_min:
                activity.duration_min = entry.duration_min
            messages += complete_activity(session, activity, user, outcome=entry.outcome, notes=entry.notes, when=when)

    if activity is None:
        activity = Activity(
            activity_type=kind, lead=lead, org_id=org.id,
            title=(entry.title or _title(kind, contacts, org))[:200],
            status="planned" if entry.status == "planned" else "done",
            scheduled_at=when if entry.status == "planned" else None,
            occurred_at=None,
            duration_min=entry.duration_min, direction=entry.direction,
            owner_user_id=entry.owner_id or user.id, outcome=(entry.outcome or None),
            notes=entry.notes or None, created_by_user_id=user.id,
        )
        for contact in contacts:
            activity.participants.append(ActivityParticipant(contact=contact))
        session.add(activity)
        session.flush()
        if entry.status == "done":
            messages += complete_activity(session, activity, user, when=when)
        else:
            messages.append(f"Planned for {when.astimezone(IST).strftime('%a %d %b, %H:%M')}.")

    if entry.script_id:
        activity.script_id = entry.script_id
    if entry.document_ids:
        from app.services.library import attach_documents  # local import: library is optional for this module

        attach_documents(session, activity, entry.document_ids)
        shared = session.scalars(select(Document.doc_type).where(Document.id.in_(entry.document_ids))).all()
        messages.append(f"{len(entry.document_ids)} document(s) recorded as shared.")
        # Sharing a brochure moves a conversation forward — but not while a cadence is running, because cadence
        # emails often carry the brochure and moving the stage would stop the follow-ups.
        if lead is not None and "Brochure" in shared and lead.cadence_status not in ("active", "awaiting_reply"):
            from app.services.cadence import advance_to

            if advance_to(session, lead, "Sent Brochure", user, why="brochure shared"):
                messages.append("Stage moved to Sent Brochure.")

    if entry.next_step is not None:
        nxt = entry.next_step
        next_kind = _kind(session, nxt.type_name)
        follow_up = Activity(
            activity_type=next_kind, lead=lead, org_id=org.id,
            title=(nxt.title or f"{next_kind.name} — follow up on {kind.name.lower()}")[:200],
            status="planned", scheduled_at=at_ist(nxt.day, nxt.at or time(10, 0)),
            direction="outbound", owner_user_id=nxt.owner_id or user.id, follow_up_of_id=activity.id,
            created_by_user_id=user.id,
        )
        for contact in contacts or [p.contact for p in activity.participants if p.contact is not None]:
            follow_up.participants.append(ActivityParticipant(contact=contact))
        session.add(follow_up)
        activity.next_step = follow_up.title
        messages.append(f"Next step planned: {follow_up.title} on {nxt.day.strftime('%a %d %b')}.")
    session.flush()
    return activity, messages


def finish_task(session: Session, activity_id: int, user: User, *, outcome: str | None = None,
                notes: str | None = None, they_replied: bool = False,
                next_step: NextStep | None = None) -> list[str]:
    """The Done button on Today: completes a planned task and optionally books what's next."""
    _require(user)
    activity = session.get(Activity, activity_id)
    if activity is None or activity.status != "planned":
        raise LogError("This task was already completed or removed — refresh the page.")
    messages = complete_activity(session, activity, user, outcome=outcome, notes=notes)
    if they_replied and activity.lead is not None:
        from app.services.cadence import on_reply  # local import keeps the module graph simple

        messages += on_reply(session, activity.lead, user)
    if next_step is not None:
        kind = _kind(session, next_step.type_name)
        follow_up = Activity(
            activity_type=kind, lead=activity.lead, org_id=activity.org_id,
            title=(next_step.title or f"{kind.name} — follow up")[:200], status="planned",
            scheduled_at=at_ist(next_step.day, next_step.at or time(10, 0)), direction="outbound",
            owner_user_id=next_step.owner_id or user.id, follow_up_of_id=activity.id, created_by_user_id=user.id,
        )
        for participant in activity.participants:
            if participant.contact is not None:
                follow_up.participants.append(ActivityParticipant(contact=participant.contact))
        session.add(follow_up)
        activity.next_step = follow_up.title
        messages.append(f"Next step planned: {follow_up.title} on {next_step.day.strftime('%a %d %b')}.")
    session.flush()
    return messages


def _may_change(activity: Activity, user: User) -> bool:
    return user.role == "admin" or user.id in (activity.owner_user_id, activity.created_by_user_id)


def update_activity(session: Session, activity_id: int, user: User, *, title: str | None = None,
                    notes: str | None = None, outcome: str | None = None, when: datetime | None = None,
                    owner_id: int | None = None) -> None:
    _require(user)
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LogError("That activity no longer exists.")
    if not _may_change(activity, user):
        raise PermissionError("Only the person who logged it, its owner or an admin can edit this.")
    before = {"title": activity.title, "notes": activity.notes, "outcome": activity.outcome}
    if title is not None and title.strip():
        activity.title = title.strip()[:200]
    if notes is not None:
        activity.notes = notes.strip() or None
    if outcome is not None:
        activity.outcome = outcome.strip()[:200] or None
    if when is not None:
        when = when if when.tzinfo else when.replace(tzinfo=IST)
        if activity.status == "planned":
            activity.scheduled_at = when
        else:
            activity.occurred_at = when
    if owner_id is not None:
        activity.owner_user_id = owner_id
    after = {"title": activity.title, "notes": activity.notes, "outcome": activity.outcome}
    diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
    if diff:
        session.add(AuditLog(user_id=user.id, entity="activities", entity_id=str(activity.id), action="update",
                             diff=diff))
    session.flush()


def delete_activity(session: Session, activity_id: int, user: User) -> None:
    _require(user)
    activity = session.get(Activity, activity_id)
    if activity is None:
        return
    if not _may_change(activity, user):
        raise PermissionError("Only the person who logged it or an admin can delete this.")
    if activity.status == "planned" and activity.cadence_step_id is not None:
        raise LogError("This is a cadence step — use Skip instead, so the cadence carries on.")
    session.add(AuditLog(user_id=user.id, entity="activities", entity_id=str(activity.id), action="delete",
                         diff={"title": activity.title, "status": activity.status,
                               "type": activity.activity_type.name if activity.activity_type else None}))
    session.delete(activity)
    session.flush()


# ---------------------------------------------------------------- Today page data


def _contact_by_activity(session: Session, activity_ids: list[int]) -> dict[int, Contact]:
    out: dict[int, Contact] = {}
    if not activity_ids:
        return out
    for activity_id, contact in session.execute(
        select(ActivityParticipant.activity_id, Contact)
        .join(Contact, Contact.id == ActivityParticipant.contact_id)
        .where(ActivityParticipant.activity_id.in_(activity_ids)).order_by(ActivityParticipant.id)
    ):
        out.setdefault(activity_id, contact)
    return out


TASK_COLUMNS = ["activity_id", "Due", "Due by", "Time", "Bucket", "Type", "channel", "Task", "Organisation",
                "org_id", "lead_id", "Track", "Stage", "Contact", "Designation", "Email", "Phone", "Owner", "owner_id",
                "Cadence", "Step", "From", "Note", "Score"]


def task_frame(session: Session, *, owner_id: int | None, today: date | None = None,
               days_ahead: int = 7) -> pd.DataFrame:
    """Planned activities up to `days_ahead` days from today. Bucket = Overdue / Due / Coming up."""
    today = today or today_ist()
    horizon = at_ist(today + timedelta(days=days_ahead + 1), time(0, 0))
    owner = aliased(User)
    creator = aliased(User)
    query = (
        select(Activity, ActivityType.name, ActivityType.channel, Organisation.name, Track.name, Stage.name,
               owner.name, CadenceStep.grace_days, CadenceStep.name, creator.name, CadenceStep.step_no,
               CadenceStep.track_id, Lead.score)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(Organisation, Organisation.id == Activity.org_id)
        .outerjoin(Lead, Lead.id == Activity.lead_id)
        .outerjoin(Track, Track.id == Lead.track_id)
        .outerjoin(Stage, Stage.id == Lead.stage_id)
        .outerjoin(owner, owner.id == Activity.owner_user_id)
        .outerjoin(creator, creator.id == Activity.created_by_user_id)
        .outerjoin(CadenceStep, CadenceStep.id == Activity.cadence_step_id)
        .where(Activity.status == "planned", Activity.scheduled_at < horizon)
        .order_by(Activity.scheduled_at, Activity.id)
    )
    if owner_id is not None:
        query = query.where(Activity.owner_user_id == owner_id)
    rows = session.execute(query).all()
    contacts = _contact_by_activity(session, [r[0].id for r in rows])
    step_totals = steps_per_track(session) if any(r[10] for r in rows) else {}
    records: list[dict[str, Any]] = []
    for (activity, type_name, channel, org_name, track, stage, owner_name, grace, step_name, creator_name,
         step_no, step_track, score) in rows:
        due = ist_date(activity.scheduled_at)
        due_by = due + timedelta(days=grace or 0)
        bucket = "Overdue" if due_by < today else ("Due" if due <= today else "Coming up")
        local = activity.scheduled_at.astimezone(IST) if activity.scheduled_at.tzinfo else activity.scheduled_at
        contact = contacts.get(activity.id)
        records.append({
            "activity_id": activity.id, "Due": due, "Due by": due_by, "Time": local.strftime("%H:%M"),
            "Bucket": bucket, "Type": type_name, "channel": channel, "Task": activity.title,
            "Organisation": org_name or "", "org_id": activity.org_id, "lead_id": activity.lead_id,
            "Track": track or "", "Stage": stage or "", "Contact": contact.name if contact else "",
            "Designation": (contact.designation or "") if contact else "",
            "Email": (contact.email or "") if contact else "", "Phone": (contact.phone or "") if contact else "",
            "Owner": owner_name or "Unassigned", "owner_id": activity.owner_user_id,
            "Cadence": step_name or "",
            "Step": f"Step {step_no} of {step_totals.get(step_track, step_no)}" if step_no else "",
            # Set by a person (not the cadence) for someone else, or handed off: show who it came from.
            "From": creator_name if (creator_name and activity.cadence_step_id is None
                                     and activity.created_by_user_id != activity.owner_user_id) else "",
            # Tagged tasks (partners, clients, content) keep their "[kind:id]" tag on the first line; never show it.
            "Note": _visible_note(activity.notes),
            "Score": int(score or 0),
        })
    return pd.DataFrame(records, columns=TASK_COLUMNS)


def ghost_frame(session: Session, *, owner_id: int | None) -> pd.DataFrame:
    """Leads whose cadence finished without a reply — Today asks whether to mark them Ghosted."""
    query = (
        select(Lead.id, Organisation.name, Track.name, Stage.name, Lead.cadence_started_on, Lead.cadence_due_on,
               User.name)
        .join(Organisation, Organisation.id == Lead.org_id)
        .join(Track, Track.id == Lead.track_id)
        .join(Stage, Stage.id == Lead.stage_id)
        .outerjoin(User, User.id == Lead.owner_user_id)
        .where(Lead.cadence_status == "finished")
        .order_by(Lead.cadence_due_on, Organisation.name)
    )
    if owner_id is not None:
        mine = select(Activity.id).where(Activity.lead_id == Lead.id, Activity.owner_user_id == owner_id,
                                         Activity.cadence_step_id.is_not(None)).exists()
        query = query.where(or_(Lead.owner_user_id == owner_id, mine))
    return pd.DataFrame(session.execute(query).all(),
                        columns=["lead_id", "Organisation", "Track", "Stage", "Cadence started", "No reply since",
                                 "Owner"])


def org_leads_and_contacts(session: Session, org_id: int) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """For the quick-log form: this organisation's leads (by track) and contacts."""
    leads = [(lid, f"{track} · {stage}") for lid, track, stage in session.execute(
        select(Lead.id, Track.name, Stage.name).join(Track, Track.id == Lead.track_id)
        .join(Stage, Stage.id == Lead.stage_id).where(Lead.org_id == org_id).order_by(Track.sort_order))]
    contacts = [(cid, " · ".join(x for x in (name, designation, email) if x)) for cid, name, designation, email in
                session.execute(select(Contact.id, Contact.name, Contact.designation, Contact.email)
                                .where(Contact.org_id == org_id).order_by(Contact.name))]
    return leads, contacts


def loggable_types(session: Session) -> list[str]:
    return [name for name in session.scalars(select(ActivityType.name).where(ActivityType.is_active.is_(True))
                                             .order_by(ActivityType.sort_order)) if name not in HIDDEN_TYPES]


def lead_timeline_activities(session: Session, org_id: int) -> list[Activity]:
    return list(session.scalars(
        select(Activity).where(Activity.org_id == org_id)
        .options(selectinload(Activity.activity_type), selectinload(Activity.owner),
                 selectinload(Activity.participants).selectinload(ActivityParticipant.contact))
        # Planned first (soonest at the top), then everything else newest first.
        .order_by(Activity.status != "planned",
                  case((Activity.status == "planned", Activity.scheduled_at)).asc(),
                  func.coalesce(Activity.occurred_at, Activity.scheduled_at, Activity.created_at).desc())
    ))


def overdue_count(session: Session, owner_id: int | None, today: date | None = None) -> int:
    frame = task_frame(session, owner_id=owner_id, today=today, days_ahead=0)
    return int((frame["Bucket"].isin(["Overdue", "Due"])).sum()) if not frame.empty else 0


__all__ = [
    "INBOUND_TYPES", "LogEntry", "LogError", "NextStep", "delete_activity", "finish_task", "ghost_frame", "lead_timeline_activities",
    "log_activity", "loggable_types", "org_leads_and_contacts", "overdue_count", "task_frame", "update_activity",
]


# Machine tags in notes ('[post:12:publish]', 'quote:14') link a task to its record; people never need to see them.
_TAG_LINE = re.compile(r"^(\[[a-z]+:[\w:]+\]|quote:\d+)$")


def strip_tags(notes: str | None) -> str:
    return "\n".join(line for line in (notes or "").splitlines() if not _TAG_LINE.match(line.strip())).strip()


def _visible_note(notes: str | None) -> str:
    lines = [line for line in (notes or "").strip().splitlines() if not _TAG_LINE.match(line.strip())]
    return lines[-1] if lines else ""
