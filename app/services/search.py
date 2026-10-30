"""Global search for the top bar: one box that finds leads, contacts and colleges."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Contact, Lead, Organisation, Stage, Track

MIN_CHARS = 2


@dataclass(frozen=True)
class Hit:
    kind: str           # "lead" / "contact" / "org"
    title: str
    detail: str
    lead_id: int | None = None
    query: str | None = None   # for pages opened with ?q=… (Colleges, Contacts)


def _like(text: str) -> str:
    # Users type plain text: escape LIKE wildcards so "%" or "_" match literally.
    cleaned = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{cleaned}%"


def search_everything(session: Session, text: str, *, limit: int = 6) -> list[Hit]:
    """Leads first (most useful), then contacts, then colleges not yet worked as leads."""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return []
    pattern = _like(text)
    hits: list[Hit] = []

    lead_rows = session.execute(
        select(Lead.id, Organisation.name, Organisation.state, Track.name, Stage.name)
        .join(Organisation, Organisation.id == Lead.org_id)
        .join(Track, Track.id == Lead.track_id)
        .join(Stage, Stage.id == Lead.stage_id)
        .where(or_(Organisation.name.ilike(pattern, escape="\\"),
                   Organisation.aishe_code.ilike(pattern, escape="\\")))
        .order_by(func.length(Organisation.name), Organisation.name)
        .limit(limit)
    ).all()
    for lead_id, name, state, track, stage in lead_rows:
        hits.append(Hit("lead", name, " · ".join(x for x in (track, stage, state) if x), lead_id=lead_id))

    # A contact opens their institution's lead when there is one (where the conversation lives).
    first_lead = (select(Lead.id).where(Lead.org_id == Contact.org_id).order_by(Lead.id).limit(1)
                  .correlate(Contact).scalar_subquery())
    contact_rows = session.execute(
        select(Contact.name, Contact.email, Contact.designation, Organisation.name, first_lead)
        .outerjoin(Organisation, Organisation.id == Contact.org_id)
        .where(or_(Contact.name.ilike(pattern, escape="\\"), Contact.email.ilike(pattern, escape="\\"),
                   Contact.phone.ilike(pattern, escape="\\")))
        .order_by(Contact.name)
        .limit(limit)
    ).all()
    for name, email, designation, org_name, lead_id in contact_rows:
        hits.append(Hit("contact", name or email or "Contact",
                        " · ".join(x for x in (designation, org_name, email) if x),
                        lead_id=lead_id, query=None if lead_id else (email or name)))

    has_lead = select(Lead.id).where(Lead.org_id == Organisation.id).exists()
    org_rows = session.execute(
        select(Organisation.name, Organisation.state, Organisation.aishe_code, Organisation.industry)
        .where(~has_lead, or_(Organisation.name.ilike(pattern, escape="\\"),
                              Organisation.aishe_code.ilike(pattern, escape="\\")))
        .order_by(func.length(Organisation.name), Organisation.name)
        .limit(limit)
    ).all()
    for name, state, code, industry in org_rows:
        hits.append(Hit("org", name, " · ".join(x for x in (industry, code, state) if x), query=name))
    return hits
