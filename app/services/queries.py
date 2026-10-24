"""The inbound query desk: enquiries from the website (hello@demo.example), email, WhatsApp, phone or events.

A new query becomes: an institution (or an 'individual'), a verified contact (they wrote to us), a lead in the
Inbound queries track, the enquiry itself on the timeline, and a cadence whose first step is
the reply — due the day it arrived, overdue after 1 day. Once qualified, the lead moves to its proper track.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    AuditLog,
    ConsentLog,
    Contact,
    Lead,
    LostReason,
    Organisation,
    Product,
    Stage,
    Suppression,
    Track,
    User,
)
from app.services.cadence import (
    IN_CADENCE,
    IST,
    change_stage,
    ist_date,
    on_reply,
    start_cadence,
    stop_cadence,
    today_ist,
)
from app.services.importing.normalise import EMAIL_RE, match_state, parse_phones
from app.services.privacy import email_hash

INBOUND_TRACK = "inbound_queries"
CHANNELS = {"Website": "Website enquiry", "Email": "Other enquiry", "WhatsApp": "Other enquiry",
            "Phone": "Other enquiry", "Event": "Other enquiry", "Other": "Other enquiry"}
QUERY_TYPES = tuple(sorted(set(CHANNELS.values())))
REPLY_TARGET = timedelta(days=1)


class QueryError(ValueError):
    """Shown to the person as-is."""


@dataclass
class NewQuery:
    channel: str
    received_at: datetime
    name: str
    email: str | None = None
    phone: str | None = None
    org_id: int | None = None  # an institution already in the hub
    new_org_name: str | None = None  # or a new one
    state: str | None = None
    products: list[str] = field(default_factory=list)
    message: str | None = None
    owner_id: int | None = None


def _require(user: User | None) -> None:
    if user is None or user.role not in ("admin", "member"):
        raise PermissionError("This needs member access.")


def create_query(session: Session, query: NewQuery, user: User) -> tuple[Lead, list[str]]:
    _require(user)
    name = (query.name or "").strip()
    email = (query.email or "").strip().lower() or None
    if not name and not email:
        raise QueryError("Add at least the person's name or email.")
    if email and not EMAIL_RE.fullmatch(email):
        raise QueryError(f"'{email}' doesn't look like an email address.")
    if email and session.scalar(select(Suppression.id).where(Suppression.email_sha256 == email_hash(email))):
        raise QueryError("This email is on the do-not-contact list.")
    if query.channel not in CHANNELS:
        raise QueryError("Pick where the query came from.")
    track = session.scalar(select(Track).where(Track.code == INBOUND_TRACK))
    if track is None:
        raise QueryError("The Inbound queries track is missing — run: python -m app.seed.seed_config")
    received = query.received_at if query.received_at.tzinfo else query.received_at.replace(tzinfo=IST)
    messages: list[str] = []

    # Institution: chosen, new, or the person themselves (an 'individual', e.g. a Mini-product buyer).
    contact = session.scalar(select(Contact).where(func.lower(Contact.email) == email)) if email else None
    org = session.get(Organisation, query.org_id) if query.org_id else None
    if org is None and query.new_org_name and query.new_org_name.strip():
        org_name = query.new_org_name.strip()
        org = session.scalar(select(Organisation).where(func.lower(Organisation.name) == org_name.lower()))
        if org is None:
            org = Organisation(name=org_name[:255], org_type="other", state=match_state(query.state))
            session.add(org)
            messages.append(f"New institution added: {org_name}.")
    if org is None and contact is not None and contact.org_id:
        org = session.get(Organisation, contact.org_id)
    if org is None:
        org = Organisation(name=(name or email)[:255], org_type="individual", state=match_state(query.state))
        session.add(org)
    session.flush()

    if contact is None:
        phone, _ = parse_phones(query.phone) if query.phone else (None, [])
        contact = Contact(org_id=org.id, name=(name or email.split("@")[0])[:160], email=email, phone=phone,
                          verified=True, verified_by_user_id=user.id, verified_on=today_ist(),
                          source=f"Query via {query.channel}")
        session.add(contact)
        session.flush()
        session.add(ConsentLog(contact=contact, source=f"They contacted us: {query.channel} query", basis="consent",
                               purpose="Replying to their enquiry and following up", captured_by_user_id=user.id))
    elif contact.org_id is None:
        contact.org_id = org.id

    lead = session.scalar(select(Lead).where(Lead.org_id == org.id, Lead.track_id == track.id))
    if lead is None:
        first = session.scalar(select(Stage).where(Stage.outcome == "open", Stage.is_active.is_(True))
                               .order_by(Stage.sort_order).limit(1))
        lead = Lead(org_id=org.id, track_id=track.id, stage=first, source=f"Query · {query.channel}",
                    owner_user_id=query.owner_id, primary_contact_id=contact.id)
        session.add(lead)
        session.flush()
        messages.append("Lead created in Inbound queries.")
    for product in session.scalars(select(Product).where(Product.name.in_(query.products))):
        if product not in lead.product_fit:
            lead.product_fit.append(product)

    kind = session.scalar(select(ActivityType).where(ActivityType.name == CHANNELS[query.channel]))
    enquiry = Activity(
        activity_type=kind, lead=lead, org_id=org.id, title=f"{query.channel} query — {contact.name}"[:200],
        status="done", occurred_at=received, direction="inbound", notes=(query.message or "").strip() or None,
        owner_user_id=query.owner_id or user.id, created_by_user_id=user.id,
    )
    enquiry.participants.append(ActivityParticipant(contact=contact))
    session.add(enquiry)
    session.add(AuditLog(user_id=user.id, entity="leads", entity_id=str(lead.id), action="create",
                         diff={"Query": [None, f"{query.channel} query from {contact.name}"], "institution": org.name}))
    session.flush()

    if lead.cadence_status in IN_CADENCE:
        messages += on_reply(session, lead, user)  # they wrote again while we were following up
    else:
        result = start_cadence(session, [lead.id], user, on=ist_date(received))
        if result.started:
            due = ist_date(received)
            messages.append(f"Reply task created — due {due.strftime('%d %b')}, overdue after 1 day.")
        elif result.skipped:
            messages.append(f"No reply task: {result.skipped[0][1]}.")
    session.flush()
    return lead, messages


def close_query(session: Session, lead_id: int, reason: str, user: User) -> None:
    """Not relevant / spam / duplicate: Rejected with that reason, cadence stopped."""
    _require(user)
    lead = session.get(Lead, lead_id)
    lost = session.scalar(select(Stage).where(Stage.outcome == "lost", Stage.is_active.is_(True))
                          .order_by(Stage.sort_order).limit(1))
    if lead is None or lost is None:
        raise QueryError("That lead no longer exists.")
    stop_cadence(session, lead, user)
    lead.lost_reason = session.scalar(select(LostReason).where(LostReason.name == reason)) or lead.lost_reason
    change_stage(session, lead, lost, user, why=reason)
    session.flush()


def move_lead_track(session: Session, lead_id: int, track_id: int, user: User) -> str:
    """Moves a lead (e.g. from Inbound queries to Mini products) once it's clear what they need."""
    _require(user)
    lead = session.get(Lead, lead_id)
    track = session.get(Track, track_id)
    if lead is None or track is None:
        raise QueryError("Pick a lead and a track.")
    if lead.track_id == track.id:
        return "Already in that track."
    clash = session.scalar(select(Lead.id).where(Lead.org_id == lead.org_id, Lead.track_id == track.id))
    if clash:
        raise QueryError(f"This institution is already a lead in {track.name} — continue there instead.")
    old = lead.track.name
    stopped = stop_cadence(session, lead, user) if lead.cadence_status in IN_CADENCE else None
    lead.track_id = track.id
    session.add(AuditLog(user_id=user.id, entity="leads", entity_id=str(lead.id), action="update",
                         diff={"Track": [old, track.name]}))
    session.flush()
    note = f"Moved from {old} to {track.name}."
    if stopped is not None:
        note += " Its cadence was stopped — start the new track's cadence from the lead when ready."
    return note


def queries_frame(session: Session, *, days: int = 90) -> pd.DataFrame:
    """One row per query received in the last `days` days, with its current status."""
    since = datetime.now(IST) - timedelta(days=days)
    rows = session.execute(
        select(Activity, Organisation.name, Lead.id, Track.name, Track.code, Stage.name, Stage.outcome,
               Lead.cadence_status, User.name)
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(Organisation, Organisation.id == Activity.org_id)
        .outerjoin(Lead, Lead.id == Activity.lead_id)
        .outerjoin(Track, Track.id == Lead.track_id)
        .outerjoin(Stage, Stage.id == Lead.stage_id)
        .outerjoin(User, User.id == Activity.owner_user_id)
        .where(ActivityType.name.in_(QUERY_TYPES), Activity.direction == "inbound", Activity.status == "done",
               Activity.occurred_at >= since)
        .order_by(Activity.occurred_at.desc())
    ).all()
    if not rows:
        return pd.DataFrame(columns=["activity_id", "lead_id", "Received", "Channel", "From", "Email", "Phone",
                                     "Institution", "Message", "Track", "Stage", "Status", "Reply due",
                                     "First reply after (h)", "Owner"])
    ids = [r[0].id for r in rows]
    people = {aid: c for aid, c in session.execute(
        select(ActivityParticipant.activity_id, Contact).join(Contact, Contact.id == ActivityParticipant.contact_id)
        .where(ActivityParticipant.activity_id.in_(ids)))}
    replies = pd.DataFrame(session.execute(
        select(Activity.org_id, Activity.occurred_at).where(
            Activity.org_id.in_({r[0].org_id for r in rows if r[0].org_id}), Activity.direction == "outbound",
            Activity.status == "done")).all(), columns=["org_id", "at"])
    now = datetime.now(IST)
    records = []
    for activity, org_name, lead_id, track, track_code, stage, outcome, cadence, owner in rows:
        received = activity.occurred_at if activity.occurred_at.tzinfo else activity.occurred_at.replace(tzinfo=IST)
        after = replies[(replies["org_id"] == activity.org_id)] if not replies.empty else replies
        later = [pd.Timestamp(t) for t in after["at"]] if not after.empty else []
        later = [t if t.tzinfo else t.tz_localize("UTC") for t in later]
        later = [t for t in later if t >= pd.Timestamp(received)]
        first_reply = min(later) if later else None
        if lead_id is None:
            status = "Not linked to a lead"
        elif outcome == "lost":
            status = "Closed"
        elif outcome == "won":
            status = "Client"
        elif first_reply is None:
            status = "Reply overdue" if now - received > REPLY_TARGET else "Awaiting reply"
        elif track_code != INBOUND_TRACK:
            status = f"Moved to {track}"
        elif cadence in ("active", "awaiting_reply", "finished"):
            status = "Following up"
        elif cadence == "replied":
            status = "In conversation"
        elif cadence == "ghosted":
            status = "Ghosted"
        else:
            status = "Replied"
        contact = people.get(activity.id)
        records.append({
            "activity_id": activity.id, "lead_id": lead_id,
            "Received": pd.Timestamp(received).tz_convert(IST).tz_localize(None),
            "Channel": activity.title.split(" query —")[0] if " query —" in activity.title else "Other",
            "From": contact.name if contact else "", "Email": (contact.email or "") if contact else "",
            "Phone": (contact.phone or "") if contact else "", "Institution": org_name or "",
            "Message": activity.notes or "", "Track": track or "", "Stage": stage or "", "Status": status,
            "Reply due": pd.Timestamp(received + REPLY_TARGET).tz_convert(IST).tz_localize(None),
            "First reply after (h)": round((first_reply - pd.Timestamp(received)).total_seconds() / 3600, 1)
            if first_reply is not None else None,
            "Owner": owner or "",
        })
    return pd.DataFrame(records)
