"""Period numbers for the Dashboard: this period vs the one before, weekly trends, team activity, upcoming demos.

All times are IST. The financial year runs April–March (Indian FY), so 'This FY' starts on 1 April.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import Activity, ActivityType, Lead, Organisation, User
from app.services.cadence import IST, at_ist, ist_date, today_ist

PERIODS = ("This week", "This month", "This quarter", "This FY")
HIDDEN_TYPES = ("Stage change",)


@dataclass(frozen=True)
class Period:
    label: str
    start: datetime
    end: datetime          # exclusive: tomorrow 00:00, so "so far"
    prev_start: datetime
    prev_end: datetime     # the same number of days, one period earlier


def _quarter_start(d: date) -> date:
    # Indian FY quarters: Apr–Jun, Jul–Sep, Oct–Dec, Jan–Mar.
    month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, month, 1)


def period_bounds(label: str, today: date | None = None) -> Period:
    today = today or today_ist()
    if label == "This week":
        start = today - timedelta(days=today.weekday())
        prev = start - timedelta(days=7)
    elif label == "This month":
        start = today.replace(day=1)
        prev = (start - timedelta(days=1)).replace(day=1)
    elif label == "This quarter":
        start = _quarter_start(today)
        prev = _quarter_start(start - timedelta(days=1))
    else:
        start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        prev = date(start.year - 1, 4, 1)
    elapsed = (today - start).days + 1
    return Period(label, at_ist(start, time(0, 0)), at_ist(today + timedelta(days=1), time(0, 0)),
                  at_ist(prev, time(0, 0)), at_ist(prev + timedelta(days=elapsed), time(0, 0)))


def _outbound_done(session: Session, start: datetime, end: datetime) -> int:
    return int(session.scalar(
        select(func.count(Activity.id)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(Activity.status == "done", Activity.direction == "outbound", ActivityType.name.not_in(HIDDEN_TYPES),
               Activity.occurred_at >= start, Activity.occurred_at < end)) or 0)


def _replies(session: Session, start: datetime, end: datetime) -> int:
    return int(session.scalar(
        select(func.count(Activity.id)).where(Activity.status == "done", Activity.direction == "inbound",
                                              Activity.occurred_at >= start, Activity.occurred_at < end)) or 0)


def _demos(session: Session, start: datetime, end: datetime) -> int:
    return int(session.scalar(
        select(func.count(Activity.id)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(ActivityType.channel == "demo", Activity.status != "cancelled",
               Activity.created_at >= start, Activity.created_at < end)) or 0)


def _new_leads(session: Session, start: datetime, end: datetime) -> int:
    return int(session.scalar(select(func.count(Lead.id)).where(Lead.created_at >= start, Lead.created_at < end))
               or 0)


def _moved_to(session: Session, stage_name: str, start: datetime, end: datetime) -> int:
    # Stage moves are written as 'Stage change' activities titled "Stage: A → B (why)".
    return int(session.scalar(
        select(func.count(Activity.id)).join(ActivityType, ActivityType.id == Activity.type_id)
        .where(ActivityType.name == "Stage change", Activity.title.like(f"%→ {stage_name}%"),
               Activity.occurred_at >= start, Activity.occurred_at < end)) or 0)


KPIS = (
    ("New leads", _new_leads, "Leads added to the hub in this period."),
    ("Outreach done", _outbound_done, "Emails, calls, WhatsApp messages, meetings and demos we did."),
    ("Replies", _replies, "Messages they sent us: replies and new enquiries."),
    ("Demos booked", _demos, "Demos planned in this period (online or offline)."),
)


def kpis(session: Session, period: Period, quote_stage: str = "Sent Quotation") -> list[dict]:
    rows = [{"label": label, "now": fn(session, period.start, period.end),
             "before": fn(session, period.prev_start, period.prev_end), "help": help_text}
            for label, fn, help_text in KPIS]
    rows.append({"label": "Quotations sent", "now": _moved_to(session, quote_stage, period.start, period.end),
                 "before": _moved_to(session, quote_stage, period.prev_start, period.prev_end),
                 "help": "Leads moved to Sent Quotation in this period."})
    return rows


def weekly_series(session: Session, weeks: int = 8, today: date | None = None) -> dict[str, list[int]]:
    """The last `weeks` Monday-to-Sunday weeks (this one included), oldest first — for the tile sparklines."""
    today = today or today_ist()
    monday = today - timedelta(days=today.weekday())
    out: dict[str, list[int]] = {label: [] for label, _, _ in KPIS}
    out["Quotations sent"] = []
    for i in range(weeks - 1, -1, -1):
        start = at_ist(monday - timedelta(days=7 * i), time(0, 0))
        end = start + timedelta(days=7)
        for label, fn, _ in KPIS:
            out[label].append(fn(session, start, end))
        out["Quotations sent"].append(_moved_to(session, "Sent Quotation", start, end))
    return out


def weekly_labels(weeks: int = 8, today: date | None = None) -> list[str]:
    """'12 Oct', '19 Oct'… — the Monday of each week in weekly_series, oldest first."""
    today = today or today_ist()
    monday = today - timedelta(days=today.weekday())
    return [f"{(monday - timedelta(days=7 * i)).day} {(monday - timedelta(days=7 * i)):%b}"
            for i in range(weeks - 1, -1, -1)]


def team_activity(session: Session, period: Period) -> pd.DataFrame:
    """Per person: what they did in the period, by channel, plus tasks overdue right now."""
    rows = session.execute(
        select(User.name, ActivityType.channel, func.count(Activity.id))
        .join(Activity, Activity.owner_user_id == User.id)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .where(User.is_active.is_(True), Activity.status == "done", Activity.direction == "outbound",
               ActivityType.name.not_in(HIDDEN_TYPES), Activity.occurred_at >= period.start,
               Activity.occurred_at < period.end)
        .group_by(User.name, ActivityType.channel)
    ).all()
    people = [name for name in session.scalars(select(User.name).where(User.is_active.is_(True)).order_by(User.id))]
    frame = pd.DataFrame(0, index=people, columns=["Emails", "Calls", "WhatsApp", "Meetings & demos", "Other"])
    bucket = {"email": "Emails", "phone": "Calls", "whatsapp": "WhatsApp", "meeting": "Meetings & demos",
              "demo": "Meetings & demos"}
    for name, channel, n in rows:
        frame.loc[name, bucket.get(channel, "Other")] += int(n)
    # Same rule as Today (grace days included), so the numbers agree with what each person sees there.
    from app.services.activity_log import task_frame  # local: activity_log imports cadence, keep startup light

    tasks = task_frame(session, owner_id=None, days_ahead=0)
    late = tasks[tasks["Bucket"] == "Overdue"].groupby("Owner").size() if not tasks.empty else pd.Series()
    frame["Overdue now"] = [int(late.get(name, 0)) for name in frame.index]
    frame["Total"] = frame[["Emails", "Calls", "WhatsApp", "Meetings & demos", "Other"]].sum(axis=1)
    return frame.reset_index(names="Person")[["Person", "Total", "Emails", "Calls", "WhatsApp", "Meetings & demos",
                                              "Overdue now"]]


def upcoming_demos(session: Session, days: int = 14, today: date | None = None) -> pd.DataFrame:
    today = today or today_ist()
    owner = aliased(User)
    rows = session.execute(
        select(Activity.scheduled_at, ActivityType.name, Organisation.name, owner.name, Activity.lead_id)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(Organisation, Organisation.id == Activity.org_id)
        .outerjoin(owner, owner.id == Activity.owner_user_id)
        .where(Activity.status == "planned", ActivityType.channel.in_(("demo", "meeting")),
               Activity.scheduled_at >= at_ist(today, time(0, 0)),
               Activity.scheduled_at < at_ist(today + timedelta(days=days + 1), time(0, 0)))
        .order_by(Activity.scheduled_at)
    ).all()
    return pd.DataFrame([{"When": pd.Timestamp(when).tz_convert(IST).tz_localize(None), "What": kind,
                          "Institution": org or "", "Who": who or "", "lead_id": lead_id}
                         for when, kind, org, who, lead_id in rows],
                        columns=["When", "What", "Institution", "Who", "lead_id"])


# ---------------------------------------------------------------- Week 8: pipeline, expiring quotes, won and lost


def expiring_quotes(session: Session, days: int = 7, today: date | None = None) -> pd.DataFrame:
    """Sent quotations whose validity ends in the next `days` days — chase them before they lapse."""
    from app.models import Opportunity, Quote

    today = today or today_ist()
    rows = session.execute(
        select(Quote.number, Quote.version, Organisation.name, Lead.id, Quote.valid_until, Quote.subtotal_inr,
               Quote.discount_inr)
        .join(Opportunity, Opportunity.id == Quote.opportunity_id).join(Lead, Lead.id == Opportunity.lead_id)
        .join(Organisation, Organisation.id == Lead.org_id)
        .where(Quote.status == "sent", Quote.valid_until >= today, Quote.valid_until <= today + timedelta(days=days))
        .order_by(Quote.valid_until)).all()
    return pd.DataFrame([{"Quote": f"{n} v{v}", "Institution": org, "lead_id": lid, "Valid until": until,
                          "Value": float((sub or 0) - (disc or 0))} for n, v, org, lid, until, sub, disc in rows],
                        columns=["Quote", "Institution", "lead_id", "Valid until", "Value"])


def won_lost_by_month(session: Session, months: int = 6, today: date | None = None) -> pd.DataFrame:
    """Deals won (by PO date) and leads lost (moved to a lost stage), per month."""
    from app.models import Opportunity, Stage
    from app.services.report_metrics import _stage_changes

    today = today or today_ist()
    first = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    labels = []
    cursor = first
    while cursor <= today:
        labels.append(cursor.strftime("%b %Y"))
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    counts = {(label, outcome): 0 for label in labels for outcome in ("Won", "Lost")}
    for (po,) in session.execute(select(Opportunity.po_date).where(Opportunity.status == "won",
                                                                   Opportunity.po_date >= first)):
        key = (po.strftime("%b %Y"), "Won")
        if key in counts:
            counts[key] += 1
    lost = set(session.scalars(select(Stage.name).where(Stage.outcome == "lost")))
    moves = _stage_changes(session, after=at_ist(first, time(0, 0)))
    for when, target in zip(moves["when"], moves["to"]) if not moves.empty else []:
        if target in lost:
            key = (ist_date(pd.Timestamp(when).to_pydatetime()).strftime("%b %Y"), "Lost")
            if key in counts:
                counts[key] += 1
    return pd.DataFrame([{"Month": label, "order": i, "Outcome": outcome, "Count": counts[(label, outcome)]}
                         for i, label in enumerate(labels) for outcome in ("Won", "Lost")])


def lost_reasons(session: Session) -> pd.DataFrame:
    """Why leads were lost: leads currently in a lost stage, by reason."""
    from app.models import LostReason, Stage

    rows = session.execute(
        select(func.coalesce(LostReason.name, "Not given"), func.count(Lead.id))
        .join(Stage, Stage.id == Lead.stage_id).outerjoin(LostReason, LostReason.id == Lead.lost_reason_id)
        .where(Stage.outcome == "lost").group_by(LostReason.name).order_by(func.count(Lead.id).desc())).all()
    return pd.DataFrame(rows, columns=["Reason", "Leads"])
