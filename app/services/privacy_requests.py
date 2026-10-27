"""DPDP requests from data principals: log them, find the person's records, act, and close within the due date.

The Digital Personal Data Protection Act, 2023 gives people the right to access, correct and erase their data and to
withdraw consent, plus grievance redressal. The hub keeps a log of every request — who asked, by which channel, when
it is due (received + `dpdp_due_days`, 30 by default) and what was done — and the actions themselves:

* Export — everything the hub holds about the email, as an Excel file to send them.
* Withdraw consent — do-not-contact on their contact and community records, consent withdrawn, email suppressed.
* Delete everywhere (admin) — their contact and community records are removed; only a one-way hash of the email
  stays on the suppression list so a later import can't bring them back.

The request log itself keeps the requester's name and email (needed to show the request was handled); delete those by
hand after your retention period if your policy says so.
"""

from __future__ import annotations

import io
import re
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    AppSetting,
    AuditLog,
    CommunityMember,
    ConsentLog,
    Contact,
    DataRequest,
    Event,
    EventParticipant,
    Organisation,
    Suppression,
    User,
)
from app.models.compliance import DATA_REQUEST_STATUSES, DATA_REQUEST_TYPES
from app.services.cadence import today_ist
from app.services.privacy import email_hash, normalise_email

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TYPE_LABELS = {"access": "Access (copy of my data)", "correction": "Correction", "deletion": "Deletion (erase me)",
               "withdrawal": "Withdraw consent / stop contacting", "grievance": "Grievance"}
STATUS_LABELS = {"open": "Open", "in_progress": "In progress", "done": "Done", "rejected": "Rejected"}
CHANNELS = ("Email", "Phone", "WhatsApp", "Letter", "In person", "Website form", "Other")
DELETION_REASON = "DPDP deletion request"
WITHDRAWAL_REASON = "DPDP: consent withdrawn"


class PrivacyError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _audit(session: Session, user: User, entity: str, entity_id: int | str, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity=entity, entity_id=str(entity_id), action=action, diff=diff))


def due_days(session: Session) -> int:
    row = session.get(AppSetting, "dpdp_due_days")
    try:
        return int(row.value) if row and row.value else 30
    except ValueError:
        return 30


def _contacts(session: Session, email: str) -> list[Contact]:
    return list(session.scalars(select(Contact).where(func.lower(func.trim(Contact.email)) == email)))


def _members(session: Session, email: str) -> list[CommunityMember]:
    return list(session.scalars(select(CommunityMember).where(
        func.lower(func.trim(CommunityMember.email)) == email)))


def log_request(session: Session, *, request_type: str, requester_name: str | None, requester_email: str | None,
                channel: str | None, received_on: date | None, notes: str | None, user: User) -> DataRequest:
    _require(user)
    if request_type not in DATA_REQUEST_TYPES:
        raise PrivacyError("Pick the kind of request.")
    email = normalise_email(requester_email) if requester_email and requester_email.strip() else None
    if email and not EMAIL_RE.match(email):
        raise PrivacyError("That email address doesn't look right.")
    if not email and not (requester_name or "").strip():
        raise PrivacyError("Give at least the person's name or email.")
    received = received_on or today_ist()
    if received > today_ist():
        raise PrivacyError("The received date can't be in the future.")
    contacts = _contacts(session, email) if email else []
    members = _members(session, email) if email else []
    request = DataRequest(
        request_type=request_type, requester_name=(requester_name or "").strip() or None, requester_email=email,
        channel=channel or None, received_on=received, due_on=received + timedelta(days=due_days(session)),
        status="open", handled_by_user_id=user.id, notes=(notes or "").strip() or None,
        contact_id=contacts[0].id if contacts else None, community_member_id=members[0].id if members else None)
    session.add(request)
    session.flush()
    _audit(session, user, "data_requests", request.id, "create", {"Request": [None, TYPE_LABELS[request_type]]})
    return request


def find_records(session: Session, email: str) -> dict:
    """Where the person appears: contacts (with institution), community, activities, consent and suppression."""
    email = normalise_email(email)
    contacts = _contacts(session, email)
    members = _members(session, email)
    contact_ids = [c.id for c in contacts]
    activities = session.scalar(select(func.count(func.distinct(ActivityParticipant.activity_id))).where(
        ActivityParticipant.contact_id.in_(contact_ids))) if contact_ids else 0
    participations = session.scalar(select(func.count(EventParticipant.id)).where(
        EventParticipant.member_id.in_([m.id for m in members]))) if members else 0
    suppression = session.scalar(select(Suppression).where(Suppression.email_sha256 == email_hash(email)))
    orgs = dict(session.execute(select(Organisation.id, Organisation.name).where(
        Organisation.id.in_([c.org_id for c in contacts if c.org_id]))).all()) if contacts else {}
    return {
        "email": email,
        "contacts": [{"id": c.id, "Name": c.name, "Institution": orgs.get(c.org_id, ""),
                      "Designation": c.designation or "", "Do not contact": c.do_not_contact} for c in contacts],
        "members": [{"id": m.id, "Name": m.name, "Institution": m.institution or "", "Level": m.level or "",
                     "Consent": m.consent, "Do not contact": m.do_not_contact} for m in members],
        "activities": int(activities or 0),
        "participations": int(participations or 0),
        "suppressed": suppression.reason if suppression else None,
    }


def _record_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame({"Note": ["Nothing held"]})


def export_person(session: Session, email: str, user: User) -> bytes:
    """An Excel copy of everything held about the email — for an access request."""
    _require(user)
    email = normalise_email(email)
    contacts = _contacts(session, email)
    members = _members(session, email)
    orgs = dict(session.execute(select(Organisation.id, Organisation.name)).all())
    contact_rows = [{
        "Name": c.name, "Institution": orgs.get(c.org_id, ""), "Designation": c.designation, "Type": c.contact_type,
        "Department": c.department, "Research interests": c.research_interests, "Email": c.email, "Phone": c.phone,
        "LinkedIn": c.linkedin_url, "Source": c.source, "Do not contact": c.do_not_contact,
        "Added": c.created_at.date() if c.created_at else None} for c in contacts]
    consent_rows = [{
        "Source": log.source, "Basis": log.basis, "Purpose": log.purpose, "Captured on": log.captured_on,
        "Withdrawn on": log.withdrawn_on} for log in session.scalars(select(ConsentLog).where(
            ConsentLog.contact_id.in_([c.id for c in contacts])))] if contacts else []
    activity_rows = [{
        "Date": (a.occurred_at or a.scheduled_at).date() if (a.occurred_at or a.scheduled_at) else None,
        "What": a.title, "Outcome": a.outcome} for a in session.scalars(
            select(Activity).join(ActivityParticipant, ActivityParticipant.activity_id == Activity.id)
            .where(ActivityParticipant.contact_id.in_([c.id for c in contacts])).distinct())] if contacts else []
    member_rows = [{
        "Name": m.name, "Email": m.email, "Phone": m.phone, "Education": m.education, "Level": m.level,
        "Institution": m.institution, "City": m.city, "State": m.state, "Interests": m.interests,
        "GitHub": m.github_url, "Website": m.website_url, "LinkedIn": m.linkedin_url, "Projects": m.project_links,
        "Consent to updates": m.consent, "Consent on": m.consent_on, "Source": m.source,
        "Do not contact": m.do_not_contact} for m in members]
    event_rows = [{
        "Event": event.name, "Date": event.start_on, "Role": part.role, "Status": part.status, "Team": part.team_name,
        "Project": part.project_title} for part, event in session.execute(
            select(EventParticipant, Event).join(Event, Event.id == EventParticipant.event_id)
            .where(EventParticipant.member_id.in_([m.id for m in members])))] if members else []
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"": [f"Personal data held by Demo for {email}", f"Prepared on {today_ist():%d %b %Y}",
                           "Activities list only what happened with you, not internal notes about other people."]}
                     ).to_excel(writer, sheet_name="About", index=False, header=False)
        for name, rows in (("Contact details", contact_rows), ("Consent", consent_rows),
                           ("Interactions", activity_rows), ("Community", member_rows), ("Events", event_rows)):
            _record_frame(rows).to_excel(writer, sheet_name=name, index=False)
    _audit(session, user, "data_requests", email_hash(email)[:12], "export", {"Export": [None, "personal data"]})
    session.flush()
    return buffer.getvalue()


def _suppress(session: Session, email: str, reason: str, user: User, *, upgrade: bool) -> None:
    hashed = email_hash(email)
    existing = session.scalar(select(Suppression).where(Suppression.email_sha256 == hashed))
    if existing is None:
        session.add(Suppression(email_sha256=hashed, reason=reason, added_by_user_id=user.id))
    elif upgrade:
        existing.reason = reason


def withdraw_consent(session: Session, email: str, user: User) -> str:
    """Stop all contact: do-not-contact everywhere, consent marked withdrawn, email suppressed. Keeps the records."""
    _require(user)
    email = normalise_email(email)
    today = today_ist()
    contacts = _contacts(session, email)
    members = _members(session, email)
    for contact in contacts:
        contact.do_not_contact = True
        contact.do_not_contact_reason = WITHDRAWAL_REASON
        contact.do_not_contact_on = today
        for log in session.scalars(select(ConsentLog).where(ConsentLog.contact_id == contact.id,
                                                            ConsentLog.withdrawn_on.is_(None))):
            log.withdrawn_on = today
    for member in members:
        member.do_not_contact = True
        member.consent = False
    _suppress(session, email, WITHDRAWAL_REASON, user, upgrade=False)
    _audit(session, user, "data_requests", email_hash(email)[:12], "update", {"Consent": [None, "withdrawn"]})
    session.flush()
    return (f"Stopped contact: {len(contacts)} contact record(s) and {len(members)} community record(s) marked "
            "do-not-contact; the email is on the suppression list.")


def delete_everywhere(session: Session, email: str, user: User) -> str:
    """Admin only: removes their contact and community records (activities lose them as a participant; the log of
    the activity itself stays for the institution). A one-way hash keeps them from being imported again."""
    _require(user, "admin")
    email = normalise_email(email)
    contacts = _contacts(session, email)
    members = _members(session, email)
    links = [DataRequest.contact_id.in_([c.id for c in contacts])] if contacts else []
    links += [DataRequest.community_member_id.in_([m.id for m in members])] if members else []
    if links:
        for request in session.scalars(select(DataRequest).where(or_(*links))):
            request.contact_id = None   # the log outlives the records it deleted
            request.community_member_id = None
    for contact in contacts:
        session.delete(contact)
    for member in members:
        session.delete(member)
    _suppress(session, email, DELETION_REASON, user, upgrade=True)
    _audit(session, user, "data_requests", email_hash(email)[:12], "delete",
           {"Erased": [f"{len(contacts)} contact(s), {len(members)} community record(s)", None]})
    session.flush()
    return (f"Deleted {len(contacts)} contact record(s) and {len(members)} community record(s). Only a one-way hash "
            "of the email remains, to block it on future imports.")


def update_request(session: Session, request_id: int, *, status: str, action_taken: str | None, user: User,
                   notes: str | None = None) -> None:
    _require(user)
    request = session.get(DataRequest, request_id)
    if request is None:
        raise PrivacyError("That request no longer exists.")
    if status not in DATA_REQUEST_STATUSES:
        raise PrivacyError("Pick a status.")
    if status in ("done", "rejected") and not (action_taken or "").strip():
        raise PrivacyError("Write what was done (or why it was rejected) before closing — it's the record.")
    before = request.status
    request.status = status
    request.action_taken = (action_taken or "").strip() or request.action_taken
    if notes is not None:
        request.notes = notes.strip() or None
    request.handled_by_user_id = user.id
    request.closed_on = today_ist() if status in ("done", "rejected") else None
    _audit(session, user, "data_requests", request.id, "update", {"Status": [before, status]})
    session.flush()


def requests_frame(session: Session, today: date | None = None) -> pd.DataFrame:
    today = today or today_ist()
    names = dict(session.execute(select(User.id, User.name)).all())
    rows = []
    for r in session.scalars(select(DataRequest).order_by(DataRequest.received_on.desc(), DataRequest.id.desc())):
        status = r.status or ("done" if r.closed_on else "open")
        open_ = status in ("open", "in_progress")
        rows.append({
            "id": r.id, "Received": r.received_on, "Due": r.due_on, "Type": TYPE_LABELS.get(r.request_type,
                                                                                             r.request_type),
            "Name": r.requester_name or "", "Email": r.requester_email or "", "Channel": r.channel or "",
            "Status": STATUS_LABELS.get(status, status), "Closed": r.closed_on,
            "Days left": (r.due_on - today).days if open_ and r.due_on else None,
            "Overdue": bool(open_ and r.due_on and r.due_on < today),
            "Handled by": names.get(r.handled_by_user_id, ""), "Action taken": r.action_taken or "",
            "Notes": r.notes or ""})
    return pd.DataFrame(rows, columns=["id", "Received", "Due", "Type", "Name", "Email", "Channel", "Status",
                                       "Closed", "Days left", "Overdue", "Handled by", "Action taken", "Notes"])


def open_counts(session: Session, today: date | None = None) -> tuple[int, int]:
    """(open, overdue) — for the Dashboard and the attention strip."""
    today = today or today_ist()
    open_filter = DataRequest.status.in_(("open", "in_progress"))
    total = session.scalar(select(func.count(DataRequest.id)).where(open_filter)) or 0
    late = session.scalar(select(func.count(DataRequest.id)).where(open_filter, DataRequest.due_on < today)) or 0
    return int(total), int(late)
