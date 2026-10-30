"""Ad-hoc tasks and handoffs, bulk actions, the daily email batch, calendar events and reply rates."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
from sqlalchemy import case, func, select, true
from sqlalchemy.orm import Session, aliased, selectinload

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    AuditLog,
    CadenceStep,
    Contact,
    Lead,
    Organisation,
    Track,
    User,
)
from app.services.cadence import IST, at_ist, complete_activity, ist_date, mark_bounced, today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}


class TaskError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None) -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < 1:
        raise PermissionError("This needs member access.")


# ---------------------------------------------------------------- tasks and handoffs


def create_task(session: Session, *, title: str, day: date, owner_id: int, user: User, at: time | None = None,
                org_id: int | None = None, lead_id: int | None = None, type_name: str = "Follow-up task",
                notes: str | None = None) -> Activity:
    """A to-do outside the cadences — for any organisation, or none ('Finalise reseller payment terms')."""
    _require(user)
    if not title or not title.strip():
        raise TaskError("Give the task a title.")
    kind = session.scalar(select(ActivityType).where(ActivityType.name == type_name))
    if kind is None or type_name == "Stage change":
        raise TaskError(f"Unknown task type '{type_name}'.")
    lead = session.get(Lead, lead_id) if lead_id else None
    task = Activity(
        activity_type=kind, lead=lead, org_id=org_id or (lead.org_id if lead else None), title=title.strip()[:200],
        status="planned", scheduled_at=at_ist(day, at or time(10, 0)),
        direction="internal" if kind.channel in ("task", "internal") else "outbound",
        owner_user_id=owner_id, notes=(notes or "").strip() or None, created_by_user_id=user.id,
    )
    if lead is not None and lead.primary_contact_id:
        task.participants.append(ActivityParticipant(contact_id=lead.primary_contact_id))
    session.add(task)
    session.flush()
    return task


def hand_off(session: Session, activity_id: int, to_user_id: int, note: str | None, user: User) -> str:
    """Passes a planned task to someone else with a note; it appears on their Today page."""
    _require(user)
    task = session.get(Activity, activity_id)
    target = session.get(User, to_user_id)
    if task is None or task.status != "planned":
        raise TaskError("This task was already completed or removed — refresh the page.")
    if target is None or not target.is_active:
        raise TaskError("Pick an active team member.")
    previous = session.get(User, task.owner_user_id).name if task.owner_user_id else "Unassigned"
    line = f"[{today_ist().strftime('%d %b')}] Handed off by {user.name} to {target.name}" + \
           (f": {note.strip()}" if note and note.strip() else "")
    task.notes = f"{task.notes}\n{line}" if task.notes else line
    task.owner_user_id = target.id
    session.add(AuditLog(user_id=user.id, entity="activities", entity_id=str(task.id), action="update",
                         diff={"Owner": [previous, target.name], "Handoff note": [None, note or ""]}))
    session.flush()
    return f"Handed off to {target.name}."


def bulk_complete(session: Session, activity_ids: list[int], user: User, *, outcome: str | None = None) -> list[str]:
    _require(user)
    done = 0
    messages: list[str] = []
    for activity in session.scalars(select(Activity).where(Activity.id.in_(activity_ids),
                                                           Activity.status == "planned")):
        messages += complete_activity(session, activity, user, outcome=outcome)
        done += 1
    return [f"{done} task(s) marked done.", *dict.fromkeys(m for m in messages if m.startswith("Next:"))][:6]


def bulk_reschedule(session: Session, activity_ids: list[int], day: date, user: User) -> int:
    _require(user)
    moved = 0
    for activity in session.scalars(select(Activity).where(Activity.id.in_(activity_ids),
                                                           Activity.status == "planned")):
        local = activity.scheduled_at.astimezone(IST).time() if activity.scheduled_at.tzinfo else time(10, 0)
        activity.scheduled_at = at_ist(day, local)
        if activity.lead is not None and activity.cadence_step_id and activity.lead.cadence_status == "active":
            activity.lead.cadence_due_on = day
        moved += 1
    session.flush()
    return moved


# ---------------------------------------------------------------- the daily email batch (for a mail-merge sheet)

EMAIL_BATCH_COLUMNS = ["Task ID", "Lead ID", "Email", "Name", "Designation", "Organisation", "Track", "Step",
                       "Sender mailbox", "Due", "Script", "Subject", "Body"]


def email_batch_frame(session: Session, owner_id: int | None, today: date | None = None) -> pd.DataFrame:
    """Planned cadence emails due today or overdue, one row per email, ready for a Google Sheet mail merge.
    Rows without a usable address are listed with Email blank so they can be fixed or skipped."""
    today = today or today_ist()
    horizon = at_ist(today + timedelta(days=1), time(0, 0))
    from app.services.library import merge_values, render, scripts_for_step  # local: keeps imports one-way

    scripts = scripts_for_step(session)
    query = (
        select(Activity, Organisation, Track.name, Track.sender_mailbox, CadenceStep.name, Lead)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(Organisation, Organisation.id == Activity.org_id)
        .outerjoin(Lead, Lead.id == Activity.lead_id)
        .outerjoin(Track, Track.id == Lead.track_id)
        .outerjoin(CadenceStep, CadenceStep.id == Activity.cadence_step_id)
        .where(Activity.status == "planned", ActivityType.channel == "email", Activity.scheduled_at < horizon)
        .options(selectinload(Activity.participants).selectinload(ActivityParticipant.contact),
                 selectinload(Activity.lead).selectinload(Lead.product_fit),
                 selectinload(Activity.lead).selectinload(Lead.owner))
        .order_by(Activity.scheduled_at, Activity.id)
    )
    if owner_id is not None:
        query = query.where(Activity.owner_user_id == owner_id)
    rows = []
    for activity, org, track, mailbox, step, lead in session.execute(query):
        org_name = org.name if org else ""
        contact = next((p.contact for p in activity.participants if p.contact is not None), None)
        usable = contact is not None and contact.email and not contact.do_not_contact \
            and contact.email_status != "bounced"
        rows.append({
            "Task ID": activity.id, "Lead ID": activity.lead_id,
            "Email": contact.email if usable else "", "Name": contact.name if contact else "",
            "Designation": (contact.designation or "") if contact else "", "Organisation": org_name or "",
            "Track": track or "", "Step": step or activity.title, "Sender mailbox": mailbox or "",
            "Due": ist_date(activity.scheduled_at), "Script": "", "Subject": "", "Body": "",
        })
        # Fill the approved script for this cadence step, so the mail-merge sheet can send it as-is.
        script = scripts.get(activity.cadence_step_id) if activity.cadence_step_id else None
        if script is not None and script.kind == "email":
            values = merge_values(contact=contact, org=org, lead=lead, sender_email=mailbox,
                                  sender_name=lead.owner.name if lead is not None and lead.owner else None)
            rows[-1].update({"Script": script.title, "Subject": render(script.subject, values)[0],
                             "Body": render(script.body, values)[0]})
    return pd.DataFrame(rows, columns=EMAIL_BATCH_COLUMNS)


def mark_batch_sent(session: Session, task_ids: list[int], user: User) -> list[str]:
    return bulk_complete(session, task_ids, user, outcome="Sent (email batch)")


def mark_task_bounced(session: Session, activity_id: int, user: User) -> list[str]:
    """The email for this task came back undeliverable."""
    _require(user)
    task = session.get(Activity, activity_id)
    if task is None:
        raise TaskError("That task no longer exists.")
    contact = next((p.contact for p in task.participants if p.contact is not None), None)
    if contact is None:
        raise TaskError("This task has no contact attached.")
    messages = mark_bounced(session, contact, user)
    if task.status == "planned":
        task.status, task.outcome = "cancelled", "Email bounced"
    session.flush()
    return messages


# ---------------------------------------------------------------- calendar

CHANNEL_COLOURS = {  # text stays white on all of these (contrast ≥ 4.5:1)
    "email": "#7A4A26", "phone": "#1B7F4C", "whatsapp": "#0F7A6B", "meeting": "#6A3FC1", "demo": "#B4235B",
    "task": "#5A6472", "web": "#A5530B", "internal": "#5A6472", "linkedin": "#6A3F1E", "other": "#5A6472",
}


def calendar_events(session: Session, start: date, end: date, *, owner_id: int | None = None,
                    types: list[str] | None = None, include_done: bool = True) -> list[dict]:
    owner = aliased(User)
    query = (
        select(Activity, ActivityType.name, ActivityType.channel, ActivityType.default_duration_min,
               Organisation.name, owner.name)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(Organisation, Organisation.id == Activity.org_id)
        .outerjoin(owner, owner.id == Activity.owner_user_id)
        .where(ActivityType.name != "Stage change", Activity.status.in_(("planned", "done") if include_done
                                                                         else ("planned",)))
    )
    lo, hi = at_ist(start, time(0, 0)), at_ist(end + timedelta(days=1), time(0, 0))
    moment = case((Activity.status == "planned", Activity.scheduled_at),
                  else_=func.coalesce(Activity.occurred_at, Activity.scheduled_at))
    query = query.where(moment >= lo, moment < hi)
    if owner_id is not None:
        query = query.where(Activity.owner_user_id == owner_id)
    if types:
        query = query.where(ActivityType.name.in_(types))
    events = []
    for activity, type_name, channel, minutes, org_name, owner_name in session.execute(query):
        moment = activity.scheduled_at if activity.status == "planned" else (activity.occurred_at or
                                                                              activity.scheduled_at)
        if moment is None:
            continue
        moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        if not (lo <= moment < hi):
            continue
        local = moment.astimezone(IST)
        length = activity.duration_min or minutes or 15
        done = activity.status == "done"
        colour = CHANNEL_COLOURS.get(channel, "#5A6472")
        events.append({
            "id": str(activity.id),
            "title": f"{'✓ ' if done else ''}{type_name}: {org_name or activity.title}",
            "start": local.replace(tzinfo=None).isoformat(timespec="minutes"),
            "end": (local + timedelta(minutes=max(length, 15))).replace(tzinfo=None).isoformat(timespec="minutes"),
            "backgroundColor": "#6B7280" if done else colour, "borderColor": "#6B7280" if done else colour,
            "textColor": "#FFFFFF",
            "extendedProps": {"activity_id": activity.id, "lead_id": activity.lead_id, "status": activity.status,
                              "type": type_name, "task": activity.title, "institution": org_name or "",
                              "owner": owner_name or "", "notes": activity.notes or ""},
        })
    return events


def _ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_for(session: Session, activity_ids: list[int]) -> bytes:
    """A calendar file (.ics) Google Calendar and Outlook can import, for the given activities."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Demo//BD Hub demo//EN", "CALSCALE:GREGORIAN"]
    query = select(Activity).where(Activity.id.in_(activity_ids)).options(
        selectinload(Activity.activity_type), selectinload(Activity.organisation))
    for activity in session.scalars(query):
        start = activity.scheduled_at or activity.occurred_at
        if start is None:
            continue
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        minutes = activity.duration_min or activity.activity_type.default_duration_min or 30
        end = start + timedelta(minutes=max(minutes, 15))
        where = activity.organisation.name if activity.organisation else ""
        lines += [
            "BEGIN:VEVENT", f"UID:bdhub-activity-{activity.id}@demo.example", f"DTSTAMP:{stamp}",
            f"DTSTART:{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{_ics_text(activity.title)}",
            f"DESCRIPTION:{_ics_text((activity.notes or '') + (chr(10) + where if where else ''))}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode()


# ---------------------------------------------------------------- reply rates (Dashboard)


def reply_rate_frame(session: Session) -> pd.DataFrame:
    """Per track: leads we contacted, how many replied afterwards, and the reply rate. Replies, not opens."""
    rows = session.execute(
        select(Lead.id, Track.name, Track.sort_order, Activity.direction, Activity.occurred_at)
        .join(Track, Track.id == Lead.track_id)
        .join(Activity, Activity.lead_id == Lead.id)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .where(Activity.status == "done", Activity.direction.in_(("inbound", "outbound")),
               ActivityType.channel.in_(("email", "phone", "whatsapp", "web", "other", "meeting", "demo")))
    ).all()
    tracks = pd.DataFrame(session.execute(select(Track.name, Track.sort_order).where(Track.is_active.is_(True))).all(),
                          columns=["Track", "order"])
    if not rows:
        out = tracks.assign(Contacted=0, Replied=0, **{"Reply rate": 0.0})
        return out.sort_values("order").drop(columns="order")
    frame = pd.DataFrame(rows, columns=["lead_id", "Track", "order", "direction", "at"])
    frame["at"] = pd.to_datetime(frame["at"], utc=True)
    first_out = frame[frame["direction"] == "outbound"].groupby("lead_id")["at"].min()
    inbound = frame[frame["direction"] == "inbound"].groupby("lead_id")["at"].max()
    replied = [lid for lid, first in first_out.items() if lid in inbound.index and inbound[lid] > first]
    per_lead = frame.drop_duplicates("lead_id")[["lead_id", "Track"]]
    per_lead = per_lead[per_lead["lead_id"].isin(first_out.index)]
    summary = per_lead.groupby("Track")["lead_id"].count().rename("Contacted").to_frame()
    summary["Replied"] = per_lead[per_lead["lead_id"].isin(replied)].groupby("Track")["lead_id"].count()
    out = tracks.merge(summary, how="left", left_on="Track", right_index=True).fillna(0)
    out["Contacted"] = out["Contacted"].astype(int)
    out["Replied"] = out["Replied"].astype(int)
    out["Reply rate"] = (out["Replied"] / out["Contacted"].where(out["Contacted"] > 0)).fillna(0.0)
    return out.sort_values("order").drop(columns="order")


def team_users(session: Session) -> list[tuple[int, str]]:
    return [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]


def contact_for_task(session: Session, activity_id: int) -> Contact | None:
    return session.scalar(select(Contact).join(ActivityParticipant, ActivityParticipant.contact_id == Contact.id)
                          .where(ActivityParticipant.activity_id == activity_id).limit(1))


# ---------------------------------------------------------------- weekly summary (Today's side panel, Dashboard)


def week_bounds(today: date | None = None) -> tuple[datetime, datetime]:
    """Monday 00:00 to next Monday 00:00, IST."""
    today = today or today_ist()
    monday = today - timedelta(days=today.weekday())
    return at_ist(monday, time(0, 0)), at_ist(monday + timedelta(days=7), time(0, 0))


def week_summary(session: Session, owner_id: int | None, today: date | None = None) -> dict[str, int]:
    """This week's work: tasks done vs still planned, replies received and demos booked."""
    start, end = week_bounds(today)
    hidden = ("Stage change",)
    mine = (Activity.owner_user_id == owner_id) if owner_id is not None else true()
    done = session.scalar(
        select(func.count(Activity.id)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(mine, Activity.status == "done", Activity.direction != "inbound", ActivityType.name.not_in(hidden),
               Activity.occurred_at >= start, Activity.occurred_at < end)) or 0
    left = session.scalar(
        select(func.count(Activity.id)).where(mine, Activity.status == "planned",
                                              Activity.scheduled_at >= start, Activity.scheduled_at < end)) or 0
    # Replies: inbound messages on leads this person owns or works on (their own tasks on that lead).
    replies_q = (select(func.count(Activity.id)).outerjoin(Lead, Lead.id == Activity.lead_id)
                 .where(Activity.direction == "inbound", Activity.status == "done",
                        Activity.occurred_at >= start, Activity.occurred_at < end))
    if owner_id is not None:
        replies_q = replies_q.where((Lead.owner_user_id == owner_id) | (Activity.owner_user_id == owner_id))
    demos = session.scalar(
        select(func.count(Activity.id)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(mine, ActivityType.channel == "demo", Activity.status != "cancelled",
               Activity.created_at >= start, Activity.created_at < end)) or 0
    return {"done": int(done), "planned": int(done) + int(left), "replies": int(session.scalar(replies_q) or 0),
            "demos": int(demos)}


# ---------------------------------------------------------------- tasks owned by a record (post-sale, content)


def plan_tagged_task(session: Session, *, tag: str, title: str, day: date, owner_id: int, user: User,
                     lead_id: int | None = None, org_id: int | None = None, note: str | None = None,
                     today: date | None = None) -> Activity | None:
    """Plans one reminder that belongs to a record (e.g. '[post:12]'): any earlier planned reminder with the same tag
    is cancelled first, so changing a date moves the task instead of adding a second one. Past dates plan nothing.
    The tag goes on the first line of the notes; the last line (shown on Today) is the human note."""
    cancel_tagged_tasks(session, tag)
    if day < (today or today_ist()):
        return None
    task = create_task(session, title=title, day=day, owner_id=owner_id, user=user, lead_id=lead_id, org_id=org_id,
                       notes=f"{tag}\n{note}" if note else tag)
    return task


def cancel_tagged_tasks(session: Session, tag: str) -> int:
    tasks = list(session.scalars(select(Activity).where(Activity.status == "planned",
                                                        Activity.notes.like(f"{tag}%"))))
    for task in tasks:
        task.status = "cancelled"
    session.flush()
    return len(tasks)


def complete_tagged_tasks(session: Session, tag: str, user: User) -> int:
    tasks = list(session.scalars(select(Activity).where(Activity.status == "planned",
                                                        Activity.notes.like(f"{tag}%"))))
    for task in tasks:
        complete_activity(session, task, user)
    session.flush()
    return len(tasks)
