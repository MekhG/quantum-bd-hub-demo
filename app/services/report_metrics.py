"""The weekly report: one Monday-to-Sunday week (IST) of BD & marketing work, for the team and senior leadership.

Definitions (also shown as help text on the page):

- Outreach: organisations contacted for the first time — leads whose very first outbound touch (email, call, WhatsApp,
  LinkedIn, meeting, demo) falls in the week. Touches on that first day count as outreach, not follow-ups.
- Follow-ups: every later outbound touch in the week, split into calls (phone and WhatsApp calls), emails,
  WhatsApp & LinkedIn messages, and meetings & demos.
- Replies: messages they sent us (replies and new enquiries).
- Leads by stage: where each lead stood at the end of the week, rebuilt from the stage-change history, so a past
  week shows what was true then.
- Deals won: purchase orders dated in the week (with value); deals lost: leads moved to a lost stage in the week.
- Pipeline: sent quotations still valid, excluding GST (and weighted by each lead's Potential).
- Segments: public / private (from the organisation's management) × urban / rural (from its area).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityType,
    AuditLog,
    Contact,
    ImportBatch,
    ImportBatchItem,
    Lead,
    LostReason,
    Opportunity,
    Organisation,
    OrgSignal,
    Quote,
    ReportNote,
    Stage,
    User,
    WeeklyReport,
    WeeklyTarget,
)
from app.services.cadence import IST, at_ist, ist_date, today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
HIDDEN_TYPES = ("Stage change",)
BUCKETS = ("Calls", "Emails", "WhatsApp & LinkedIn", "Meetings & demos", "Other")
POTENTIAL_WEIGHT = {"0%": 0.0, "30-50%": 0.4, "50-70%": 0.6, "70-90%": 0.8, "100%": 1.0}

# What a weekly target can be set on. Keys are stored in weekly_targets.metric.
TARGET_METRICS: dict[str, str] = {
    "new_leads": "New leads added",
    "contacts_verified": "Contacts verified",
    "outreach": "Outreach (first contact)",
    "followup_emails": "Follow-up emails",
    "followup_calls": "Follow-up calls",
    "replies": "Replies",
    "demos": "Demos held",
    "quotes": "Quotations sent",
}


class ReportError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


# ---------------------------------------------------------------- weeks


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def default_week(today: date | None = None) -> date:
    """The week the report is usually about: the last full week (on Monday you report on the week just ended)."""
    return monday_of(today or today_ist()) - timedelta(days=7)


def week_range(week_start: date) -> tuple[datetime, datetime]:
    return at_ist(week_start, time(0, 0)), at_ist(week_start + timedelta(days=7), time(0, 0))


def week_label(week_start: date) -> str:
    end = week_start + timedelta(days=6)
    if week_start.month == end.month:
        return f"{week_start.day}–{end.day} {end:%b %Y}"
    return f"{week_start.day} {week_start:%b} – {end.day} {end:%b %Y}"


def _fy_start(day: date) -> date:
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


# ---------------------------------------------------------------- touches: outreach and follow-ups


def bucket_for(channel: str, type_name: str) -> str:
    if channel == "phone" or "call" in (type_name or "").lower():
        return "Calls"
    if channel == "email":
        return "Emails"
    if channel in ("whatsapp", "linkedin"):
        return "WhatsApp & LinkedIn"
    if channel in ("meeting", "demo"):
        return "Meetings & demos"
    return "Other"


def _first_touch_days(session: Session) -> tuple[dict[int, date], dict[int, date]]:
    """First outbound touch day per lead, and per organisation for touches logged without a lead."""
    done = (Activity.status == "done", Activity.direction == "outbound", ActivityType.name.not_in(HIDDEN_TYPES))
    by_lead = {lid: ist_date(when) for lid, when in session.execute(
        select(Activity.lead_id, func.min(Activity.occurred_at)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(*done, Activity.lead_id.is_not(None)).group_by(Activity.lead_id))}
    by_org = {oid: ist_date(when) for oid, when in session.execute(
        select(Activity.org_id, func.min(Activity.occurred_at)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(*done, Activity.lead_id.is_(None), Activity.org_id.is_not(None)).group_by(Activity.org_id))}
    return by_lead, by_org


def touches(session: Session, start: datetime, end: datetime,
            first: tuple[dict[int, date], dict[int, date]] | None = None) -> pd.DataFrame:
    """Outbound touches done in [start, end), each marked Outreach or Follow-up, with its channel bucket."""
    by_lead, by_org = first or _first_touch_days(session)
    rows = session.execute(
        select(Activity.id, Activity.lead_id, Activity.org_id, Activity.owner_user_id, Activity.occurred_at,
               ActivityType.channel, ActivityType.name)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .where(Activity.status == "done", Activity.direction == "outbound", ActivityType.name.not_in(HIDDEN_TYPES),
               Activity.occurred_at >= start, Activity.occurred_at < end)).all()
    records = []
    for aid, lead_id, org_id, owner_id, when, channel, type_name in rows:
        day = ist_date(when)
        first_day = by_lead.get(lead_id) if lead_id is not None else by_org.get(org_id)
        key = f"lead:{lead_id}" if lead_id is not None else f"org:{org_id}"
        records.append({"id": aid, "key": key, "lead_id": lead_id, "org_id": org_id, "owner_id": owner_id,
                        "day": day, "bucket": bucket_for(channel, type_name),
                        "kind": "Outreach" if first_day is not None and day == first_day else "Follow-up"})
    return pd.DataFrame(records, columns=["id", "key", "lead_id", "org_id", "owner_id", "day", "bucket", "kind"])


def _touch_summary(frame: pd.DataFrame) -> dict[str, int]:
    follow = frame[frame["kind"] == "Follow-up"]
    out = {"outreach": int(frame.loc[frame["kind"] == "Outreach", "key"].nunique()),
           "outreach_messages": int((frame["kind"] == "Outreach").sum()),
           "followups": len(follow)}
    for name in BUCKETS:
        out[f"followup:{name}"] = int((follow["bucket"] == name).sum())
    return out


# ---------------------------------------------------------------- the other numbers


def _replies(session: Session, start: datetime, end: datetime) -> pd.DataFrame:
    rows = session.execute(
        select(Activity.id, Activity.lead_id, Activity.owner_user_id, Lead.owner_user_id)
        .outerjoin(Lead, Lead.id == Activity.lead_id)
        .where(Activity.direction == "inbound", Activity.status == "done",
               Activity.occurred_at >= start, Activity.occurred_at < end)).all()
    return pd.DataFrame([{"id": a, "lead_id": lid, "person": lead_owner or owner} for a, lid, owner, lead_owner in rows],
                        columns=["id", "lead_id", "person"])


def _demos(session: Session, start: datetime, end: datetime) -> pd.DataFrame:
    rows = session.execute(
        select(Activity.id, Activity.lead_id, Activity.owner_user_id)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .where(ActivityType.channel == "demo", Activity.status == "done",
               Activity.occurred_at >= start, Activity.occurred_at < end)).all()
    return pd.DataFrame(rows, columns=["id", "lead_id", "person"])


def _quotes_sent(session: Session, first_day: date, last_day: date) -> pd.DataFrame:
    rows = session.execute(
        select(Quote.id, Opportunity.lead_id, Quote.created_by_user_id, Quote.subtotal_inr, Quote.discount_inr)
        .join(Opportunity, Opportunity.id == Quote.opportunity_id)
        .where(Quote.sent_on >= first_day, Quote.sent_on <= last_day)).all()
    return pd.DataFrame([{"id": q, "lead_id": lid, "person": who, "value": float((sub or 0) - (disc or 0))}
                         for q, lid, who, sub, disc in rows], columns=["id", "lead_id", "person", "value"])


def _new_leads(session: Session, start: datetime, end: datetime) -> pd.DataFrame:
    """Leads created in the week, credited to whoever imported or added them."""
    created = {lid for lid in session.scalars(select(Lead.id).where(Lead.created_at >= start, Lead.created_at < end))}
    credit: dict[int, int | None] = {lid: None for lid in created}
    for lid, who in session.execute(
            select(ImportBatchItem.entity_id, ImportBatch.imported_by_user_id)
            .join(ImportBatch, ImportBatch.id == ImportBatchItem.batch_id)
            .where(ImportBatchItem.entity == "leads", ImportBatch.undone_at.is_(None),
                   ImportBatchItem.entity_id.in_(created))) if created else []:
        credit[lid] = who
    for entity_id, who in session.execute(
            select(AuditLog.entity_id, AuditLog.user_id)
            .where(AuditLog.entity == "leads", AuditLog.action == "create",
                   AuditLog.entity_id.in_([str(c) for c in created]))) if created else []:
        credit[int(entity_id)] = credit.get(int(entity_id)) or who
    return pd.DataFrame([{"lead_id": lid, "person": who} for lid, who in credit.items()],
                        columns=["lead_id", "person"])


def _verified(session: Session, first_day: date, last_day: date) -> pd.DataFrame:
    rows = session.execute(select(Contact.id, Contact.verified_by_user_id)
                           .where(Contact.verified.is_(True), Contact.verified_on >= first_day,
                                  Contact.verified_on <= last_day)).all()
    return pd.DataFrame(rows, columns=["id", "person"])


def _stage_changes(session: Session, after: datetime | None = None) -> pd.DataFrame:
    """Every stage change ('Stage: A → B'), oldest first."""
    query = (select(Activity.lead_id, Activity.occurred_at, Activity.title)
             .join(ActivityType, ActivityType.id == Activity.type_id)
             .where(ActivityType.name == "Stage change", Activity.lead_id.is_not(None))
             .order_by(Activity.occurred_at, Activity.id))
    if after is not None:
        query = query.where(Activity.occurred_at >= after)
    records = []
    for lead_id, when, title in session.execute(query):
        body = (title or "").removeprefix("Stage:").strip()
        if "→" not in body:
            continue
        before, _, rest = body.partition("→")
        target = rest.split(" (", 1)[0].strip()
        records.append({"lead_id": lead_id, "when": when, "from": before.strip(), "to": target})
    return pd.DataFrame(records, columns=["lead_id", "when", "from", "to"])


def stages_at(session: Session, start: datetime, end: datetime) -> pd.DataFrame:
    """Leads at each stage at the end of the week, and how many moved into each stage during it."""
    stages = session.execute(select(Stage.name, Stage.outcome, Stage.sort_order).where(Stage.is_active.is_(True))
                             .order_by(Stage.sort_order)).all()
    leads = session.execute(select(Lead.id, Stage.name).join(Stage, Stage.id == Lead.stage_id)
                            .where(Lead.created_at < end)).all()
    later = _stage_changes(session, after=end)
    first_later = later.drop_duplicates("lead_id").set_index("lead_id")["from"] if not later.empty else pd.Series()
    at_end: dict[str, int] = {}
    for lead_id, current in leads:
        name = first_later.get(lead_id, current) if lead_id in first_later.index else current
        if name and name != "—":
            at_end[name] = at_end.get(name, 0) + 1
    moves = _stage_changes(session, after=start)
    moves = moves[pd.to_datetime(moves["when"], utc=True) < pd.Timestamp(end)] if not moves.empty else moves
    moved = moves.groupby("to").size().to_dict() if not moves.empty else {}
    return pd.DataFrame([{"Stage": name, "Leads": at_end.get(name, 0), "Moved in this week": int(moved.get(name, 0)),
                          "outcome": outcome, "order": order} for name, outcome, order in stages],
                        columns=["Stage", "Leads", "Moved in this week", "outcome", "order"])


def _deals(session: Session, start: datetime, end: datetime, first_day: date, last_day: date
           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    won = session.execute(
        select(Organisation.name, Opportunity.title, Opportunity.value_inr, Opportunity.po_date)
        .join(Lead, Lead.id == Opportunity.lead_id).join(Organisation, Organisation.id == Lead.org_id)
        .where(Opportunity.status == "won", Opportunity.po_date >= first_day, Opportunity.po_date <= last_day)
        .order_by(Opportunity.po_date)).all()
    won_frame = pd.DataFrame([{"Organisation": n, "Deal": t, "Value (₹)": float(v or 0), "PO date": d}
                              for n, t, v, d in won], columns=["Organisation", "Deal", "Value (₹)", "PO date"])
    lost_names = set(session.scalars(select(Stage.name).where(Stage.outcome == "lost")))
    moves = _stage_changes(session, after=start)
    if not moves.empty:
        moves = moves[(pd.to_datetime(moves["when"], utc=True) < pd.Timestamp(end)) & moves["to"].isin(lost_names)]
    lost_rows = []
    for lead_id in dict.fromkeys(moves["lead_id"].tolist() if not moves.empty else []):
        row = session.execute(select(Organisation.name, LostReason.name, Lead.lost_note)
                              .join(Organisation, Organisation.id == Lead.org_id)
                              .outerjoin(LostReason, LostReason.id == Lead.lost_reason_id)
                              .where(Lead.id == lead_id)).first()
        if row:
            lost_rows.append({"Organisation": row[0], "Reason": row[1] or "Not given", "Note": row[2] or ""})
    return won_frame, pd.DataFrame(lost_rows, columns=["Organisation", "Reason", "Note"])


def pipeline_now(session: Session, today: date | None = None) -> tuple[float, float, int]:
    """Sent quotations still valid, excluding GST: (total, weighted by Potential, how many)."""
    today = today or today_ist()
    rows = session.execute(
        select(Quote.subtotal_inr, Quote.discount_inr, Lead.potential)
        .join(Opportunity, Opportunity.id == Quote.opportunity_id).join(Lead, Lead.id == Opportunity.lead_id)
        .where(Quote.status == "sent", Quote.valid_until >= today)).all()
    total = sum(float((s or 0) - (d or 0)) for s, d, _ in rows)
    weighted = sum(float((s or 0) - (d or 0)) * POTENTIAL_WEIGHT.get(p or "", 0.5) for s, d, p in rows)
    return total, weighted, len(rows)


# ---------------------------------------------------------------- segments


def sector_of(management: str | None) -> str:
    text = management.lower() if isinstance(management, str) else ""
    if "private" in text:
        return "Private"
    if any(word in text for word in ("government", "govt", "central", "state", "public", "municipal", "deemed")):
        return "Public"
    return "Not set"


def area_of(area: str | None) -> str:
    text = area.strip().lower() if isinstance(area, str) else ""
    return "Rural" if text.startswith("rural") else "Urban" if text.startswith("urban") else "Not set"


def segments(session: Session, start: datetime, end: datetime, week: pd.DataFrame, replies: pd.DataFrame,
             demos: pd.DataFrame, today: date) -> pd.DataFrame:
    """Public / private × urban / rural: the week's activity, this FY's quotes and wins, and the pipeline now."""
    leads = pd.DataFrame(session.execute(
        select(Lead.id, Organisation.management, Organisation.area, Stage.outcome)
        .join(Organisation, Organisation.id == Lead.org_id).join(Stage, Stage.id == Lead.stage_id)).all(),
        columns=["lead_id", "management", "area", "outcome"])
    if leads.empty:
        return pd.DataFrame()
    leads["Segment"] = [f"{sector_of(m)} · {area_of(a)}" for m, a in zip(leads["management"], leads["area"])]
    segment_of = leads.set_index("lead_id")["Segment"]
    fy_first = _fy_start(today)
    quotes = _quotes_sent(session, fy_first, today)
    won = pd.DataFrame(session.execute(
        select(Opportunity.lead_id, Opportunity.value_inr).where(Opportunity.status == "won",
                                                                 Opportunity.po_date >= fy_first)).all(),
        columns=["lead_id", "value"])
    pipe = pd.DataFrame(session.execute(
        select(Opportunity.lead_id, Quote.subtotal_inr, Quote.discount_inr)
        .join(Opportunity, Opportunity.id == Quote.opportunity_id)
        .where(Quote.status == "sent", Quote.valid_until >= today)).all(), columns=["lead_id", "sub", "disc"])

    def by_segment(frame: pd.DataFrame, value: str | None = None, distinct: bool = True) -> pd.Series:
        if frame.empty:
            return pd.Series(dtype=float)
        seg = frame["lead_id"].map(segment_of)
        if value is not None:
            return frame.assign(seg=seg).groupby("seg")[value].sum()
        return frame.assign(seg=seg).groupby("seg")["lead_id"].nunique() if distinct else seg.value_counts()

    contacted = by_segment(week[week["lead_id"].notna()]) if not week.empty else pd.Series(dtype=float)
    pipe_values = pipe.assign(value=[float((s or 0) - (d or 0)) for s, d in zip(pipe["sub"], pipe["disc"])]) \
        if not pipe.empty else pipe
    won_values = won.assign(value=won["value"].astype(float).fillna(0)) if not won.empty else won
    order = ["Public · Urban", "Public · Rural", "Private · Urban", "Private · Rural"]
    names = order + sorted(s for s in leads["Segment"].unique() if s not in order)
    table = pd.DataFrame({
        "Segment": names,
        "Leads": [int((leads["Segment"] == n).sum()) for n in names],
        "Contacted (week)": [int(contacted.get(n, 0)) for n in names],
        "Replied (week)": [int(by_segment(replies[replies["lead_id"].notna()]).get(n, 0)) if not replies.empty
                           else 0 for n in names],
        "Demos (week)": [int(by_segment(demos[demos["lead_id"].notna()]).get(n, 0)) if not demos.empty else 0
                         for n in names],
        "Quotes sent (FY)": [int(by_segment(quotes, distinct=False).get(n, 0)) if not quotes.empty else 0
                             for n in names],
        "Won (FY)": [int(by_segment(won, distinct=False).get(n, 0)) if not won.empty else 0 for n in names],
        "Won value (FY, ₹)": [float(by_segment(won_values, "value").get(n, 0)) if not won.empty else 0.0
                              for n in names],
        "Pipeline now (₹)": [float(by_segment(pipe_values, "value").get(n, 0)) if not pipe.empty else 0.0
                             for n in names],
    })
    return table[(table["Leads"] > 0)].reset_index(drop=True)


# ---------------------------------------------------------------- people and targets


def _team(session: Session) -> list[tuple[int, str]]:
    return [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True),
                                                                       User.role != "viewer").order_by(User.id))]


def people_actuals(session: Session, week_start: date, *, first=None) -> pd.DataFrame:
    """One row per team member: the week's numbers for every target metric."""
    start, end = week_range(week_start)
    last_day = week_start + timedelta(days=6)
    week = touches(session, start, end, first)
    follow = week[week["kind"] == "Follow-up"]
    outreach = week[week["kind"] == "Outreach"].drop_duplicates("key")
    replies, demos = _replies(session, start, end), _demos(session, start, end)
    quotes, new, verified = _quotes_sent(session, week_start, last_day), _new_leads(session, start, end), \
        _verified(session, week_start, last_day)

    def count(frame: pd.DataFrame, column: str, uid: int) -> int:
        return int((frame[column] == uid).sum()) if not frame.empty else 0

    rows = []
    for uid, name in _team(session):
        rows.append({
            "user_id": uid, "Person": name,
            "new_leads": count(new, "person", uid), "contacts_verified": count(verified, "person", uid),
            "outreach": count(outreach, "owner_id", uid),
            "followup_emails": int(((follow["owner_id"] == uid) & (follow["bucket"] == "Emails")).sum()),
            "followup_calls": int(((follow["owner_id"] == uid) & (follow["bucket"] == "Calls")).sum()),
            "followup_other": int(((follow["owner_id"] == uid) & ~follow["bucket"].isin(["Emails", "Calls"])).sum()),
            "replies": count(replies, "person", uid), "demos": count(demos, "person", uid),
            "quotes": count(quotes, "person", uid),
        })
    return pd.DataFrame(rows)


def latest_target_week(session: Session, before: date) -> date | None:
    return session.scalar(select(func.max(WeeklyTarget.week_start)).where(WeeklyTarget.week_start < before))


def targets_frame(session: Session, week_start: date) -> tuple[pd.DataFrame, bool]:
    """The targets grid (one row per person, one column per metric). If this week has none yet, it starts from the
    most recent week that has targets — returns (frame, copied_from_earlier)."""
    rows = session.execute(select(WeeklyTarget.user_id, WeeklyTarget.metric, WeeklyTarget.target)
                           .where(WeeklyTarget.week_start == week_start)).all()
    copied = False
    if not rows:
        earlier = latest_target_week(session, week_start)
        if earlier is not None:
            rows = session.execute(select(WeeklyTarget.user_id, WeeklyTarget.metric, WeeklyTarget.target)
                                   .where(WeeklyTarget.week_start == earlier)).all()
            copied = bool(rows)
    values = {(uid, metric): target for uid, metric, target in rows}
    frame = pd.DataFrame([{"user_id": uid, "Person": name,
                           **{label: values.get((uid, key)) for key, label in TARGET_METRICS.items()}}
                          for uid, name in _team(session)])
    return frame, copied


def save_targets(session: Session, week_start: date, frame: pd.DataFrame, user: User) -> int:
    """Replaces the week's targets with the grid. Blank = no target."""
    _require(user, "admin")
    if week_start.weekday() != 0:
        raise ReportError("Targets are set per week, starting on a Monday.")
    label_to_key = {label: key for key, label in TARGET_METRICS.items()}
    existing = {(t.user_id, t.metric): t for t in session.scalars(
        select(WeeklyTarget).where(WeeklyTarget.week_start == week_start))}
    keep: set[tuple[int, str]] = set()
    changed = 0
    for row in frame.to_dict("records"):
        uid = int(row["user_id"])
        for label, key in label_to_key.items():
            value = row.get(label)
            if value is None or value != value or str(value).strip() == "":
                continue
            try:
                target = int(float(value))
            except (TypeError, ValueError) as error:
                raise ReportError(f"{row.get('Person')}: '{value}' isn't a whole number.") from error
            if target < 0 or target > 100000:
                raise ReportError(f"{row.get('Person')}: targets must be between 0 and 100,000.")
            keep.add((uid, key))
            current = existing.get((uid, key))
            if current is None:
                session.add(WeeklyTarget(week_start=week_start, user_id=uid, metric=key, target=target))
                changed += 1
            elif current.target != target:
                current.target = target
                changed += 1
    for pair, row in existing.items():
        if pair not in keep:
            session.delete(row)
            changed += 1
    if changed:
        session.add(AuditLog(user_id=user.id, entity="weekly_targets", entity_id=week_start.isoformat(),
                             action="update", diff={"Targets": [None, f"{changed} change(s)"]}))
        session.flush()
    return changed


def targets_progress(session: Session, week_start: date, actuals: pd.DataFrame | None = None) -> pd.DataFrame:
    """Target vs actual for every target set in the week."""
    rows = session.execute(select(WeeklyTarget.user_id, WeeklyTarget.metric, WeeklyTarget.target, User.name)
                           .join(User, User.id == WeeklyTarget.user_id)
                           .where(WeeklyTarget.week_start == week_start).order_by(User.id)).all()
    if not rows:
        return pd.DataFrame(columns=["Person", "Metric", "Target", "Actual", "Progress"])
    actuals = actuals if actuals is not None else people_actuals(session, week_start)
    by_user = actuals.set_index("user_id") if not actuals.empty else pd.DataFrame()
    order = list(TARGET_METRICS)
    records = []
    for uid, metric, target, name in sorted(rows, key=lambda r: (r[0], order.index(r[1]) if r[1] in order else 99)):
        actual = int(by_user.loc[uid, metric]) if uid in by_user.index and metric in by_user.columns else 0
        records.append({"Person": name, "Metric": TARGET_METRICS.get(metric, metric), "Target": int(target),
                        "Actual": actual, "Progress": (actual / target) if target else 1.0, "metric": metric,
                        "user_id": uid})
    return pd.DataFrame(records)


def my_week(session: Session, user_id: int, today: date | None = None) -> pd.DataFrame:
    """This week's targets and progress for one person (Today's side panel)."""
    week_start = monday_of(today or today_ist())
    progress = targets_progress(session, week_start)
    return progress[progress["user_id"] == user_id] if not progress.empty else progress


# ---------------------------------------------------------------- notes


def get_note(session: Session, user_id: int, week_start: date) -> str:
    return session.scalar(select(ReportNote.text).where(ReportNote.user_id == user_id,
                                                        ReportNote.week_start == week_start)) or ""


def save_note(session: Session, week_start: date, text: str, user: User) -> None:
    """Each person writes their own note for the week's report."""
    _require(user)
    note = session.scalar(select(ReportNote).where(ReportNote.user_id == user.id, ReportNote.week_start == week_start))
    text = (text or "").strip()
    if note is None:
        if text:
            session.add(ReportNote(user_id=user.id, week_start=week_start, text=text[:4000]))
    else:
        note.text = text[:4000] or None
    session.flush()


def notes_for_week(session: Session, week_start: date) -> list[dict]:
    return [{"Person": name, "Note": text} for name, text in session.execute(
        select(User.name, ReportNote.text).join(User, User.id == ReportNote.user_id)
        .where(ReportNote.week_start == week_start, ReportNote.text.is_not(None)).order_by(User.id))]


# ---------------------------------------------------------------- the report


HEADLINES = (
    ("outreach", "Outreach", "Organisations contacted for the first time this week."),
    ("followup:Calls", "Follow-up calls", "Phone and WhatsApp calls after the first contact."),
    ("followup:Emails", "Follow-up emails", "Emails after the first contact."),
    ("replies", "Replies", "Messages they sent us: replies and new enquiries."),
    ("demos", "Demos held", "Online and offline demos done this week."),
    ("quotes", "Quotations sent", "Quotations marked as sent this week."),
    ("won", "Deals won", "Purchase orders dated this week."),
    ("new_leads", "New leads", "Leads added to the hub this week."),
)


def _numbers(session: Session, week_start: date, first) -> dict:
    start, end = week_range(week_start)
    last_day = week_start + timedelta(days=6)
    week = touches(session, start, end, first)
    summary = _touch_summary(week)
    replies, demos = _replies(session, start, end), _demos(session, start, end)
    quotes = _quotes_sent(session, week_start, last_day)
    won = session.execute(select(func.count(Opportunity.id), func.coalesce(func.sum(Opportunity.value_inr), 0))
                          .where(Opportunity.status == "won", Opportunity.po_date >= week_start,
                                 Opportunity.po_date <= last_day)).one()
    contacted = week["key"].nunique() if not week.empty else 0
    replied = replies["lead_id"].dropna().nunique() if not replies.empty else 0
    return {**summary, "replies": len(replies), "replied_leads": int(replied), "contacted_leads": int(contacted),
            "demos": len(demos), "quotes": len(quotes), "quotes_value": float(quotes["value"].sum()) if not
            quotes.empty else 0.0, "won": int(won[0]), "won_value": float(won[1] or 0),
            "new_leads": int(session.scalar(select(func.count(Lead.id)).where(Lead.created_at >= start,
                                                                                Lead.created_at < end)) or 0),
            "_week": week, "_replies": replies, "_demos": demos}


def build_report(session: Session, week_start: date, today: date | None = None) -> dict:
    """Everything the report shows, as plain values (so it can be frozen as JSON when the report is finalised)."""
    today = today or today_ist()
    if week_start.weekday() != 0:
        week_start = monday_of(week_start)
    first = _first_touch_days(session)
    start, end = week_range(week_start)
    now = _numbers(session, week_start, first)
    before = _numbers(session, week_start - timedelta(days=7), first)
    headline = [{"key": key, "label": label, "now": int(now[key]), "before": int(before[key]), "help": help_text}
                for key, label, help_text in HEADLINES]
    followups = [{"Channel": name, "This week": now[f"followup:{name}"], "Last week": before[f"followup:{name}"]}
                 for name in BUCKETS if now[f"followup:{name}"] or before[f"followup:{name}"]]
    stage_frame = stages_at(session, start, end)
    won, lost = _deals(session, start, end, week_start, week_start + timedelta(days=6))
    is_current = week_start == monday_of(today)
    as_of = min(today, week_start + timedelta(days=6))
    total, weighted, open_quotes = pipeline_now(session, as_of if is_current else today)
    actuals = people_actuals(session, week_start, first=first)
    progress = targets_progress(session, week_start, actuals)
    people = actuals.rename(columns={"new_leads": "New leads", "contacts_verified": "Contacts verified",
                                     "outreach": "Outreach", "followup_calls": "Follow-up calls",
                                     "followup_emails": "Follow-up emails", "followup_other": "Other follow-ups",
                                     "replies": "Replies", "demos": "Demos", "quotes": "Quotes"}) \
        if not actuals.empty else actuals
    segment_frame = segments(session, start, end, now["_week"], now["_replies"], now["_demos"], today)

    labels, outreach_trend, follow_trend, reply_trend = [], [], [], []
    for i in range(7, -1, -1):
        monday = week_start - timedelta(days=7 * i)
        s, e = week_range(monday)
        frame = touches(session, s, e, first)
        summary = _touch_summary(frame)
        labels.append(f"{monday.day} {monday:%b}")
        outreach_trend.append(summary["outreach"])
        follow_trend.append(summary["followups"])
        reply_trend.append(len(_replies(session, s, e)))

    from app.services.signals import hot_leads

    hot = hot_leads(session, limit=8)
    new_signals = int(session.scalar(select(func.count(OrgSignal.id)).where(
        OrgSignal.created_at >= start, OrgSignal.created_at < end)) or 0)

    def records(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
        if frame is None or frame.empty:
            return []
        out = frame[columns].copy()
        for column in out.columns:
            if out[column].dtype == object:
                out[column] = out[column].map(lambda v: v.isoformat() if isinstance(v, date) else v)
        return out.to_dict("records")

    reply_rate, cohort = _reply_rate(session, end, first)
    return {
        "week_start": week_start.isoformat(), "label": week_label(week_start), "is_current": is_current,
        "generated_at": datetime.now(timezone.utc).astimezone(IST).strftime("%d %b %Y, %H:%M"),
        "headline": headline, "followups": followups,
        "totals": {"outreach_messages": now["outreach_messages"], "followups": now["followups"],
                   "followups_before": before["followups"], "contacted_leads": now["contacted_leads"],
                   "replied_leads": now["replied_leads"], "reply_rate": reply_rate, "reply_cohort": cohort,
                   "quotes_value": now["quotes_value"], "won_value": now["won_value"],
                   "pipeline": total, "pipeline_weighted": weighted, "open_quotes": open_quotes,
                   "new_signals": new_signals, "pipeline_as_of": (as_of if is_current else today).isoformat()},
        "stages": records(stage_frame, ["Stage", "Leads", "Moved in this week", "outcome"]),
        "won": records(won, ["Organisation", "Deal", "Value (₹)", "PO date"]),
        "lost": records(lost, ["Organisation", "Reason", "Note"]),
        "people": records(people, ["Person", "New leads", "Contacts verified", "Outreach", "Follow-up calls",
                                   "Follow-up emails", "Other follow-ups", "Replies", "Demos", "Quotes"]),
        "targets": records(progress, ["Person", "Metric", "Target", "Actual", "Progress"]),
        "segments": records(segment_frame, list(segment_frame.columns)) if not segment_frame.empty else [],
        "trend": {"labels": labels, "Outreach": outreach_trend, "Follow-ups": follow_trend, "Replies": reply_trend},
        "hot": records(hot, ["Organisation", "Track", "Stage", "Score"]),
        "notes": notes_for_week(session, week_start),
        "community": _community(session, week_start),
        "content": _content(session, week_start),
    }


def _community(session: Session, week_start: date) -> dict:
    from app.services.community import community_week

    return community_week(session, week_start, week_start + timedelta(days=6))


def _content(session: Session, week_start: date) -> dict:
    """LinkedIn / Instagram: posts this week, the approval queue and which themes do best (last 12 weeks).
    Plain values only — a finalised report is stored as JSON."""
    from app.services.content import content_week

    data = content_week(session, week_start, week_start + timedelta(days=6))
    themes = data.pop("themes")
    best = data.pop("best_post")
    data["best_post"] = {"Post": best[0], "Engagements": int(best[1]), "Theme": best[2]} if best else None
    measured = themes[themes["With numbers"] > 0]
    data["themes"] = [{k: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for k, v in row.items()}
                      for row in measured.to_dict("records")]
    return data


def _reply_rate(session: Session, end: datetime, first, weeks: int = 4) -> tuple[float | None, int]:
    """Of the leads first contacted in the 4 weeks up to the week's end, the share that have written back since.
    A cohort, so it doesn't swing with the timing of a single week."""
    by_lead, _ = first
    last_day = ist_date(end - timedelta(seconds=1))
    cohort = [lid for lid, day in by_lead.items() if last_day - timedelta(days=7 * weeks) < day <= last_day]
    if not cohort:
        return None, 0
    replied = set(session.scalars(select(Activity.lead_id).where(
        Activity.lead_id.in_(cohort), Activity.direction == "inbound", Activity.status == "done",
        Activity.occurred_at < end)))
    return len(replied) / len(cohort), len(cohort)


# ---------------------------------------------------------------- saved reports


def get_report_row(session: Session, week_start: date) -> WeeklyReport | None:
    return session.scalar(select(WeeklyReport).where(WeeklyReport.week_start == week_start))


def report_for(session: Session, week_start: date, today: date | None = None) -> tuple[dict, WeeklyReport | None]:
    """The frozen numbers of a final report, or live numbers for a draft — plus the saved comments row."""
    row = get_report_row(session, week_start)
    if row is not None and row.status == "final" and row.snapshot:
        return row.snapshot, row
    return build_report(session, week_start, today), row


def save_comments(session: Session, week_start: date, *, highlights: str, risks: str, asks: str,
                  user: User) -> WeeklyReport:
    _require(user, "admin")
    row = get_report_row(session, week_start)
    if row is None:
        row = WeeklyReport(week_start=week_start, status="draft", created_by_user_id=user.id)
        session.add(row)
    if row.status == "final":
        raise ReportError("This report is final. Reopen it to change the comments.")
    row.highlights, row.risks, row.asks = (highlights or "").strip() or None, (risks or "").strip() or None, \
        (asks or "").strip() or None
    session.flush()
    return row


def finalise(session: Session, week_start: date, user: User, today: date | None = None) -> WeeklyReport:
    """Freezes the numbers, so what leadership saw never changes when activities are edited later."""
    _require(user, "admin")
    row = get_report_row(session, week_start)
    if row is None:
        row = WeeklyReport(week_start=week_start, created_by_user_id=user.id)
        session.add(row)
    row.snapshot = build_report(session, week_start, today)
    row.status, row.finalised_at, row.finalised_by_user_id = "final", datetime.now(timezone.utc), user.id
    session.add(AuditLog(user_id=user.id, entity="weekly_reports", entity_id=week_start.isoformat(),
                         action="update", diff={"Report": ["draft", "final"]}))
    session.flush()
    return row


def reopen(session: Session, week_start: date, user: User) -> None:
    _require(user, "admin")
    row = get_report_row(session, week_start)
    if row is None or row.status != "final":
        raise ReportError("Only a final report can be reopened.")
    row.status, row.snapshot, row.finalised_at = "draft", None, None
    session.add(AuditLog(user_id=user.id, entity="weekly_reports", entity_id=week_start.isoformat(),
                         action="update", diff={"Report": ["final", "draft"]}))
    session.flush()


def history_frame(session: Session) -> pd.DataFrame:
    rows = session.execute(select(WeeklyReport.week_start, WeeklyReport.status, WeeklyReport.finalised_at, User.name)
                           .outerjoin(User, User.id == WeeklyReport.finalised_by_user_id)
                           .order_by(WeeklyReport.week_start.desc())).all()
    return pd.DataFrame([{"Week": week_label(w), "week_start": w, "Status": "Final" if s == "final" else "Draft",
                          "Finalised": f.astimezone(IST).strftime("%d %b %Y, %H:%M") if f else "",
                          "By": who or ""} for w, s, f, who in rows],
                        columns=["Week", "week_start", "Status", "Finalised", "By"])


# ---------------------------------------------------------------- the Monday task


def ensure_monday_task(session: Session, today: date | None = None) -> int:
    """Once a week, each admin gets 'Prepare last week's report and set this week's targets' on Today
    (due Monday; created on the first visit of the week)."""
    from app.services.tasks import create_task

    today = today or today_ist()
    monday = monday_of(today)
    title = f"Prepare last week's report and set this week's targets ({week_label(monday - timedelta(days=7))})"
    created = 0
    for admin in session.scalars(select(User).where(User.role == "admin", User.is_active.is_(True))):
        exists = session.scalar(select(Activity.id).where(Activity.owner_user_id == admin.id, Activity.title == title))
        if exists:
            continue
        create_task(session, title=title, day=today, owner_id=admin.id, user=admin,
                    notes="Open Weekly report from the menu: check the numbers, add comments, finalise and download. "
                          "Then set this week's targets on the Targets tab.")
        created += 1
    return created
