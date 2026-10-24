"""The cadence engine: plans each lead's follow-ups from its track's cadence steps.

Life of a lead in a cadence (cadence_status):
  start (by hand, or automatically once a contact is verified)      → active, step 1 planned
  every activity of a step done or skipped                          → next step planned N days later
  last step done                                                    → awaiting_reply (wait one more gap)
  wait over, still no reply                                         → finished  (Today: "Mark as Ghosted?")
  they reply (inbound activity logged)                              → replied   (planned steps cancelled,
                                                                                 stage → Replied to Queries)
  someone stops it / marks Ghosted / keeps it open                  → stopped / ghosted / kept_open

The engine never sends anything: a planned "Email · 1st follow-up" is a task for a person.
All dates are Indian Standard Time.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    AuditLog,
    CadenceStep,
    Contact,
    Lead,
    Stage,
    TaskAssignment,
    Track,
    User,
)

IST = ZoneInfo("Asia/Kolkata")
TASK_HOUR = time(10, 0)  # planned tasks show at 10:00 IST
IN_CADENCE = ("active", "awaiting_reply", "finished")
STATUS_LABELS = {
    "active": "In cadence",
    "awaiting_reply": "Waiting for reply",
    "finished": "Finished — no reply",
    "replied": "Replied",
    "stopped": "Stopped",
    "ghosted": "Ghosted",
    "kept_open": "Ended, kept open",
}


def today_ist() -> date:
    return datetime.now(IST).date()


def at_ist(day: date, at: time = TASK_HOUR) -> datetime:
    return datetime.combine(day, at, tzinfo=IST)


def ist_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite returns naive UTC
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(IST).date()


# ---------------------------------------------------------------- helpers


def _steps(session: Session, track_id: int) -> list[CadenceStep]:
    return list(session.scalars(
        select(CadenceStep).where(CadenceStep.track_id == track_id).order_by(CadenceStep.step_no)
        .options(selectinload(CadenceStep.activity_types))
    ))


def _stage(session: Session, name: str) -> Stage | None:
    return session.scalar(select(Stage).where(func.lower(Stage.name) == name.lower(), Stage.is_active.is_(True)))


def _first_stage(session: Session) -> Stage:
    return session.scalar(select(Stage).where(Stage.outcome == "open", Stage.is_active.is_(True))
                          .order_by(Stage.sort_order).limit(1))


def _audit(session: Session, user: User | None, lead: Lead, field_name: str, old: str | None, new: str) -> None:
    session.add(AuditLog(user_id=user.id if user else None, entity="leads", entity_id=str(lead.id), action="update",
                         diff={field_name: [old, new]}))


def change_stage(session: Session, lead: Lead, stage: Stage, user: User | None, *, why: str | None = None) -> bool:
    """Sets the stage, notes when, and writes it on the timeline. Returns False if nothing changed."""
    old = lead.stage
    if old is not None and old.id == stage.id:
        return False
    lead.stage = stage
    lead.stage_changed_at = datetime.now(IST)
    kind = session.scalar(select(ActivityType).where(ActivityType.name == "Stage change"))
    if kind is not None:
        session.add(Activity(
            activity_type=kind, lead=lead, org_id=lead.org_id,
            title=f"Stage: {old.name if old else '—'} → {stage.name}" + (f" ({why})" if why else ""),
            status="done", occurred_at=datetime.now(IST), direction="internal",
            owner_user_id=user.id if user else None, created_by_user_id=user.id if user else None,
        ))
    _audit(session, user, lead, "Stage", old.name if old else None, stage.name)
    return True


def advance_to(session: Session, lead: Lead, stage_name: str, user: User | None, *, why: str) -> bool:
    """Moves forward only (or revives a Ghosted lead); never touches a won or rejected lead."""
    target = _stage(session, stage_name)
    current = lead.stage
    if target is None or current is None or current.outcome in ("won", "lost"):
        return False
    if current.outcome == "nurture" or (current.outcome == "open" and target.sort_order > current.sort_order):
        return change_stage(session, lead, target, user, why=why)
    return False


class _Assigner:
    """Who gets each planned task: the admin's task assignments → the lead's owner → whoever started it."""

    def __init__(self, session: Session) -> None:
        self.rules: dict[tuple[int, int], int | None] = {
            (a.track_id, a.activity_type_id): a.user_id for a in session.scalars(select(TaskAssignment))
        }
        self.active = set(session.scalars(select(User.id).where(User.is_active.is_(True))))

    def pick(self, lead: Lead, kind: ActivityType, starter: User | None) -> int | None:
        for candidate in (self.rules.get((lead.track_id, kind.id)), lead.owner_user_id,
                          starter.id if starter else None):
            if candidate in self.active:
                return candidate
        return None


def _reachable(contact: Contact | None) -> bool:
    return contact is not None and not contact.do_not_contact and contact.email_status != "bounced"


NOT_BOUNCED = or_(Contact.email_status.is_(None), Contact.email_status != "bounced")


def _main_contact(session: Session, lead: Lead) -> Contact | None:
    if lead.primary_contact_id:
        contact = session.get(Contact, lead.primary_contact_id)
        if _reachable(contact):
            return contact
    return session.scalar(
        select(Contact).where(Contact.org_id == lead.org_id, Contact.do_not_contact.is_(False), NOT_BOUNCED)
        .order_by(Contact.verified.desc(), Contact.email.is_(None), Contact.id).limit(1)
    )


def _plan_step(session: Session, lead: Lead, step: CadenceStep, day: date, starter: User | None,
               assigner: _Assigner) -> list[Activity]:
    contact = _main_contact(session, lead)
    planned = []
    for kind in step.activity_types:
        activity = Activity(
            activity_type=kind, lead=lead, org_id=lead.org_id, title=f"{kind.name} · {step.name}",
            status="planned", scheduled_at=at_ist(day), direction="outbound",
            owner_user_id=assigner.pick(lead, kind, starter), cadence_step=step,
            created_by_user_id=starter.id if starter else None,
        )
        if contact is not None:
            activity.participants.append(ActivityParticipant(contact=contact))
        session.add(activity)
        planned.append(activity)
    lead.current_step_no = step.step_no
    lead.cadence_due_on = day
    return planned


# ---------------------------------------------------------------- starting


@dataclass
class StartResult:
    started: list[tuple[int, date]] = field(default_factory=list)  # (lead id, first step day)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (institution, reason)

    def by_day(self) -> dict[date, int]:
        return dict(sorted(Counter(day for _, day in self.started).items()))

    def summary(self) -> str:
        if not self.started:
            return "No cadences started."
        days = self.by_day()
        parts = [f"{n} on {d.strftime('%d %b')}" for d, n in days.items()]
        return f"Cadence started for {len(self.started)} lead(s): " + ", ".join(parts) + "."


def has_verified_contact(session: Session, lead: Lead) -> bool:
    return bool(session.scalar(select(exists().where(
        Contact.org_id == lead.org_id, Contact.verified.is_(True), Contact.do_not_contact.is_(False),
        Contact.email.is_not(None), NOT_BOUNCED,
    ))))


def why_not_startable(session: Session, lead: Lead, steps: list[CadenceStep]) -> str | None:
    if not steps:
        return "this track has no cadence steps"
    if lead.cadence_status in IN_CADENCE:
        return "already in a cadence"
    if lead.stage is not None and lead.stage.outcome == "won":
        return "already a client"
    if lead.stage is not None and lead.stage.outcome == "lost":
        return "marked Rejected"
    if not has_verified_contact(session, lead):
        return "no verified contact with an email (verify one on the Contacts page)"
    return None


def start_cadence(session: Session, lead_ids: list[int], user: User | None, *, on: date | None = None,
                  auto: bool = False) -> StartResult:
    """Starts the cadence for these leads. With a daily cap, extra leads start on the following days
    (by hand) or wait for the next day (automatic starts)."""
    result = StartResult()
    first_day = on or today_ist()
    leads = list(session.scalars(select(Lead).where(Lead.id.in_(lead_ids)).options(
        selectinload(Lead.organisation), selectinload(Lead.stage), selectinload(Lead.track))))
    assigner = _Assigner(session)
    steps_by_track: dict[int, list[CadenceStep]] = {}
    booked: dict[tuple[int, date], int] = {}  # (track id, day) → starts already booked

    def capacity(track: Track, day: date) -> int | None:
        if not track.daily_start_cap:
            return None
        key = (track.id, day)
        if key not in booked:
            booked[key] = session.scalar(select(func.count(Lead.id)).where(
                Lead.track_id == track.id, Lead.cadence_started_on == day)) or 0
        return track.daily_start_cap - booked[key]

    for lead in sorted(leads, key=lambda item: item.id):
        steps = steps_by_track.setdefault(lead.track_id, _steps(session, lead.track_id))
        reason = why_not_startable(session, lead, steps)
        if reason:
            result.skipped.append((lead.organisation.name, reason))
            continue
        day = first_day
        while (left := capacity(lead.track, day)) is not None and left <= 0:
            if auto:
                break
            day += timedelta(days=1)
        if auto and (left := capacity(lead.track, day)) is not None and left <= 0:
            result.skipped.append((lead.organisation.name, "daily cap reached — starts on a later day"))
            continue
        if lead.track.daily_start_cap:
            booked[(lead.track_id, day)] = booked.get((lead.track_id, day), 0) + 1
        _plan_step(session, lead, steps[0], day, user, assigner)
        old = STATUS_LABELS.get(lead.cadence_status or "", None)
        lead.cadence_status = "active"
        lead.cadence_started_on = day
        lead.cadence_ended_on = None
        _audit(session, user, lead, "Cadence", old,
               f"{'Auto-started' if auto else 'Started'} · step 1 on {day.strftime('%d %b %Y')}")
        result.started.append((lead.id, day))
    session.flush()
    return result


# ---------------------------------------------------------------- working through steps


def _step_activities(session: Session, lead: Lead, step: CadenceStep) -> list[Activity]:
    """This run's activities for a step (a restarted cadence ignores the earlier run)."""
    query = select(Activity).where(Activity.lead_id == lead.id, Activity.cadence_step_id == step.id)
    if lead.cadence_started_on:
        query = query.where(Activity.scheduled_at >= at_ist(lead.cadence_started_on, time(0, 0)))
    return list(session.scalars(query))


def _after_step_progress(session: Session, lead: Lead, step: CadenceStep, user: User | None) -> list[str]:
    if lead.cadence_status != "active" or lead.current_step_no != step.step_no:
        return []
    activities = _step_activities(session, lead, step)
    handled = [a for a in activities if a.status != "planned"]
    if step.complete_on_any:
        if not any(a.status == "done" for a in handled) and len(handled) < len(activities):
            return []
        for leftover in (a for a in activities if a.status == "planned"):
            leftover.status, leftover.outcome = "cancelled", "Not needed — step already done"
    elif len(handled) < len(activities):
        return []

    steps = _steps(session, lead.track_id)
    later = [s for s in steps if s.step_no > step.step_no]
    today = today_ist()
    if later:
        nxt = later[0]
        day = today + timedelta(days=nxt.days_after_previous)
        _plan_step(session, lead, nxt, day, user, _Assigner(session))
        return [f"Next: {nxt.name} planned for {day.strftime('%a %d %b')}."]
    wait = max((step.days_after_previous or 0) + (step.grace_days or 0), 3)
    lead.cadence_status = "awaiting_reply"
    lead.cadence_due_on = today + timedelta(days=wait)
    return [f"Last step done. If there's no reply by {lead.cadence_due_on.strftime('%d %b')}, "
            "Today will suggest marking it Ghosted."]


def complete_activity(session: Session, activity: Activity, user: User | None, *, outcome: str | None = None,
                      notes: str | None = None, when: datetime | None = None) -> list[str]:
    activity.status = "done"
    activity.occurred_at = when or datetime.now(IST)
    if outcome:
        activity.outcome = outcome[:200]
    if notes:
        activity.notes = f"{activity.notes}\n{notes}" if activity.notes else notes
    messages: list[str] = []
    lead = activity.lead
    if lead is not None and activity.direction == "outbound" and \
            activity.activity_type.channel in ("email", "phone", "whatsapp"):
        if _answers_their_message(session, activity):
            # They wrote first (a query or a reply) and we have now answered.
            if advance_to(session, lead, "Replied to Queries", user, why="we replied to their query"):
                messages.append("Stage moved to Replied to Queries.")
        elif lead.stage_id == _first_stage(session).id and advance_to(session, lead, "First Contact Made", user,
                                                                         why="first contact"):
            messages.append("Stage moved to First Contact Made.")
    if lead is not None and activity.direction == "inbound":
        messages += on_reply(session, lead, user)
    if lead is not None and activity.cadence_step is not None:
        messages += _after_step_progress(session, lead, activity.cadence_step, user)
    session.flush()
    return messages


def _answers_their_message(session: Session, activity: Activity) -> bool:
    """True when the institution's latest message to us is newer than anything we've sent since."""
    if activity.org_id is None:
        return False
    last_in = session.scalar(select(func.max(Activity.occurred_at)).where(
        Activity.org_id == activity.org_id, Activity.direction == "inbound", Activity.status == "done"))
    if last_in is None:
        return False
    answered = session.scalar(select(exists().where(
        Activity.org_id == activity.org_id, Activity.direction == "outbound", Activity.status == "done",
        Activity.id != activity.id, Activity.occurred_at >= last_in)))
    return not answered


def mark_bounced(session: Session, contact: Contact, user: User | None) -> list[str]:
    """An email to this contact came back undeliverable: flag it, unverify it, and stop cadences that now have
    nobody to email. Cadences with another verified contact carry on with that person."""
    messages = [f"{contact.name}: email marked as bounced and unverified."]
    old = contact.email_status
    contact.email_status, contact.verified = "bounced", False
    session.add(AuditLog(user_id=user.id if user else None, entity="contacts", entity_id=str(contact.id),
                         action="update", diff={"Email status": [old, "bounced"], "Verified": [True, False]}))
    session.flush()
    if contact.org_id is None:
        return messages
    for lead in session.scalars(select(Lead).where(Lead.org_id == contact.org_id,
                                                   Lead.cadence_status.in_(IN_CADENCE))):
        if lead.primary_contact_id == contact.id:
            lead.primary_contact_id = None
        if not has_verified_contact(session, lead):
            _cancel_planned_steps(session, lead, "Cadence stopped — email bounced, no other verified contact")
            _audit(session, user, lead, "Cadence", STATUS_LABELS[lead.cadence_status], "Stopped — email bounced")
            lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "stopped", today_ist(), None
            messages.append(f"Cadence stopped for {lead.track.name} (no other verified contact).")
        else:
            replacement = _main_contact(session, lead)
            for planned in session.scalars(select(Activity).where(Activity.lead_id == lead.id,
                                                                  Activity.status == "planned")):
                planned.participants[:] = [p for p in planned.participants if p.contact_id != contact.id]
                if replacement is not None and not planned.participants:
                    planned.participants.append(ActivityParticipant(contact=replacement))
            messages.append(f"{lead.track.name}: cadence continues with {replacement.name if replacement else '—'}.")
    session.flush()
    return messages


def skip_activity(session: Session, activity: Activity, user: User | None, reason: str | None = None) -> list[str]:
    activity.status = "cancelled"
    activity.outcome = f"Skipped{': ' + reason if reason else ''}"[:200]
    messages = []
    if activity.lead is not None and activity.cadence_step is not None:
        messages = _after_step_progress(session, activity.lead, activity.cadence_step, user)
    session.flush()
    return messages


def reschedule_activity(session: Session, activity: Activity, day: date, at: time | None = None) -> None:
    current = activity.scheduled_at
    keep_time = at or (current.astimezone(IST).time() if current and current.tzinfo else TASK_HOUR)
    activity.scheduled_at = at_ist(day, keep_time)
    lead = activity.lead
    if lead is not None and activity.cadence_step is not None and lead.cadence_status == "active":
        lead.cadence_due_on = day
    session.flush()


# ---------------------------------------------------------------- ending


def _cancel_planned_steps(session: Session, lead: Lead, why: str) -> int:
    planned = list(session.scalars(select(Activity).where(
        Activity.lead_id == lead.id, Activity.status == "planned", Activity.cadence_step_id.is_not(None))))
    for activity in planned:
        activity.status, activity.outcome = "cancelled", why[:200]
    return len(planned)


def on_reply(session: Session, lead: Lead, user: User | None) -> list[str]:
    """They replied: stop the cadence and move to Replied to Queries (forward only)."""
    messages = []
    if lead.cadence_status in IN_CADENCE:
        cancelled = _cancel_planned_steps(session, lead, "Cadence stopped — they replied")
        _audit(session, user, lead, "Cadence", STATUS_LABELS[lead.cadence_status], "Stopped — they replied")
        lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "replied", today_ist(), None
        messages.append("Cadence stopped because they replied"
                        + (f" ({cancelled} planned step(s) cancelled)." if cancelled else "."))
    if advance_to(session, lead, "Replied to Queries", user, why="they replied"):
        messages.append("Stage moved to Replied to Queries.")
    return messages


def stop_cadence(session: Session, lead: Lead, user: User | None) -> int:
    if lead.cadence_status not in IN_CADENCE:
        return 0
    cancelled = _cancel_planned_steps(session, lead, f"Cadence stopped by {user.name if user else 'the app'}")
    _audit(session, user, lead, "Cadence", STATUS_LABELS[lead.cadence_status], "Stopped")
    lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "stopped", today_ist(), None
    session.flush()
    return cancelled


def mark_ghosted(session: Session, lead: Lead, user: User | None) -> None:
    ghosted = _stage(session, "Ghosted") or session.scalar(
        select(Stage).where(Stage.outcome == "nurture", Stage.is_active.is_(True)).order_by(Stage.sort_order).limit(1))
    _cancel_planned_steps(session, lead, "Marked Ghosted")
    if ghosted is not None:
        change_stage(session, lead, ghosted, user, why="no reply to the cadence")
    lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "ghosted", today_ist(), None
    session.flush()


def keep_open(session: Session, lead: Lead, user: User | None) -> None:
    _audit(session, user, lead, "Cadence", STATUS_LABELS.get(lead.cadence_status or ""), "Ended, kept open")
    lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "kept_open", today_ist(), None
    session.flush()


# ---------------------------------------------------------------- daily housekeeping


@dataclass
class DailyCheck:
    now_finished: int = 0
    auto_started: StartResult = field(default_factory=StartResult)


def run_daily_checks(session: Session, *, today: date | None = None, limit: int = 300) -> DailyCheck:
    """Cheap and safe to run on every Today page load:
    1. leads whose post-cadence wait is over become 'finished' (Today suggests Ghosted);
    2. tracks set to auto-start begin the cadence for New leads that now have a verified contact."""
    today = today or today_ist()
    check = DailyCheck()
    waiting = session.scalars(select(Lead).where(Lead.cadence_status == "awaiting_reply",
                                                 Lead.cadence_due_on <= today))
    for lead in waiting:
        lead.cadence_status = "finished"
        check.now_finished += 1

    auto_tracks = list(session.scalars(select(Track.id).where(Track.auto_start_cadence.is_(True),
                                                              Track.is_active.is_(True))))
    if auto_tracks:
        first = _first_stage(session)
        verified = exists().where(Contact.org_id == Lead.org_id, Contact.verified.is_(True),
                                  Contact.do_not_contact.is_(False), Contact.email.is_not(None), NOT_BOUNCED)
        query = (select(Lead.id).where(Lead.track_id.in_(auto_tracks), Lead.cadence_status.is_(None),
                                       Lead.stage_id == first.id, verified)
                 .order_by(Lead.id).limit(limit))
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)  # two people opening Today at once won't double-start
        ids = list(session.scalars(query))
        if ids:
            check.auto_started = start_cadence(session, ids, None, on=today, auto=True)
    session.flush()
    return check


def cadence_label(status: str | None, step_no: int | None, total: int | None, due: date | None) -> str:
    if status == "active":
        due_text = f" · due {due.strftime('%d %b')}" if due else ""
        step = int(step_no) if step_no is not None and step_no == step_no else 1  # NaN-safe
        return f"Step {step}/{total or '?'}{due_text}"
    return STATUS_LABELS.get(status or "", "")


def steps_per_track(session: Session) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for track_id, n in session.execute(select(CadenceStep.track_id, func.count(CadenceStep.id))
                                       .group_by(CadenceStep.track_id)):
        counts[track_id] = n
    return counts


def stop_if_conversation_moved(session: Session, lead: Lead, user: User | None) -> bool:
    """A stage beyond First Contact Made (or won / rejected / ghosted) means the conversation has moved on,
    so automatic follow-ups would be wrong: stop the cadence."""
    if lead.cadence_status not in IN_CADENCE or lead.stage is None:
        return False
    contacted = _stage(session, "First Contact Made")
    moved = lead.stage.outcome != "open" or (contacted is not None and lead.stage.sort_order > contacted.sort_order)
    if not moved:
        return False
    _cancel_planned_steps(session, lead, f"Cadence stopped — stage moved to {lead.stage.name}")
    _audit(session, user, lead, "Cadence", STATUS_LABELS[lead.cadence_status], f"Stopped — stage {lead.stage.name}")
    lead.cadence_status, lead.cadence_ended_on, lead.cadence_due_on = "stopped", today_ist(), None
    return True
