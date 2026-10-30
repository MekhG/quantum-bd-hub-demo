"""Events & community: events, the Google Forms import, the community directory, mailing lists, turning a member's
organisation into a lead, and event-participation signals.

Mailing rule (decided 2026-10-21): a member is mailable only with consent, an 18-or-older confirmation and no
do-not-contact. Everyone else stays on the event record but never appears in mailing lists or outreach exports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CommunityMember,
    Event,
    EventParticipant,
    ImportBatch,
    ImportBatchItem,
    Lead,
    OptionList,
    Organisation,
    OrgSignal,
    SignalType,
    Stage,
    Suppression,
    Track,
    User,
)
from app.models.community import EVENT_MODES, EVENT_STATUSES, PARTICIPANT_ROLES
from app.services.cadence import ist_date, today_ist
from app.services.importing.normalise import default_industry, guess_org_type
from app.services.privacy import email_hash
from app.services.signals import add_signal, name_variants, normalise, recompute_scores

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^https?://\S+$")
SIGNAL_NAME = "Event participation"
ERASED_REASON = "Community: deletion request"
MAX_UPLOAD_BYTES = 15_000_000

STATUS_LABELS = {"registered": "Registered", "attended": "Attended", "no_show": "No-show", "finalist": "Finalist",
                 "winner": "Winner"}
ROLE_LABELS = {r: r.title() for r in PARTICIPANT_ROLES}


class CommunityError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _audit(session: Session, user: User, entity: str, entity_id: int | str, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity=entity, entity_id=str(entity_id), action=action, diff=diff))


def _text(value, limit: int | None = None) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in ("nan", "none", "n/a", "na", "-"):
        return None
    return text[:limit] if limit else text


# ---------------------------------------------------------------- events


@dataclass
class EventData:
    name: str
    kind: str
    start_on: date
    end_on: date | None = None
    mode: str = "online"
    venue: str | None = None
    partner: str | None = None
    form_url: str | None = None
    cost_inr: str | float | None = None
    status: str = "planned"
    owner_id: int | None = None
    description: str | None = None
    notes: str | None = None


def save_event(session: Session, data: EventData, user: User, event_id: int | None = None) -> Event:
    _require(user)
    name = (data.name or "").strip()
    if not name:
        raise CommunityError("Give the event a name.")
    if not data.kind:
        raise CommunityError("Pick the type of event.")
    if data.end_on and data.end_on < data.start_on:
        raise CommunityError("The end date is before the start date.")
    if data.mode not in EVENT_MODES or data.status not in EVENT_STATUSES:
        raise CommunityError("Pick a valid mode and status.")
    url = (data.form_url or "").strip() or None
    if url and not URL_RE.match(url):
        raise CommunityError("The form link must start with https://")
    cost = None
    if data.cost_inr not in (None, ""):
        try:
            cost = Decimal(str(data.cost_inr).replace(",", "").replace("₹", "").strip())
        except InvalidOperation as error:
            raise CommunityError("Cost must be a number (₹).") from error
    event = session.get(Event, event_id) if event_id else Event()
    if event is None:
        raise CommunityError("That event no longer exists.")
    before = event.status if event_id else None
    event.name, event.kind, event.start_on, event.end_on = name[:200], data.kind, data.start_on, data.end_on
    event.mode, event.venue, event.partner, event.form_url = data.mode, _text(data.venue, 255), \
        _text(data.partner, 120), url
    event.cost_inr, event.status, event.owner_user_id = cost, data.status, data.owner_id or user.id
    event.description, event.notes = (data.description or "").strip() or None, (data.notes or "").strip() or None
    if not event_id:
        session.add(event)
    session.flush()
    _audit(session, user, "events", event.id, "update" if event_id else "create", {"Event": [before, event.status]})
    if event.status == "done" and before != "done":
        sync_event_signals(session, event.id, user)
    return event


def events_frame(session: Session) -> pd.DataFrame:
    counts = {eid: (n, teams, attended, winners) for eid, n, teams, attended, winners in session.execute(
        select(EventParticipant.event_id, func.count(EventParticipant.id),
               func.count(func.distinct(EventParticipant.team_name)),
               func.sum(case((EventParticipant.status.in_(("attended", "finalist", "winner")), 1), else_=0)),
               func.sum(case((EventParticipant.status == "winner", 1), else_=0)))
        .group_by(EventParticipant.event_id))}
    rows = session.execute(select(Event, User.name).outerjoin(User, User.id == Event.owner_user_id)
                           .order_by(Event.start_on.desc(), Event.id.desc())).all()
    return pd.DataFrame([{
        "id": e.id, "Event": e.name, "Type": e.kind, "Starts": e.start_on, "Ends": e.end_on or e.start_on,
        "Mode": e.mode.title(), "Venue": e.venue or "", "Partner": e.partner or "",
        "Status": e.status.title(), "Participants": int(counts.get(e.id, (0, 0, 0, 0))[0]),
        "Teams": int(counts.get(e.id, (0, 0, 0, 0))[1] or 0),
        "Attended": int(counts.get(e.id, (0, 0, 0, 0))[2] or 0), "Owner": who or "",
        "Form": e.form_url or "",
    } for e, who in rows], columns=["id", "Event", "Type", "Starts", "Ends", "Mode", "Venue", "Partner", "Status",
                                    "Participants", "Teams", "Attended", "Owner", "Form"])


def delete_event(session: Session, event_id: int, user: User) -> str:
    """Admin only. Participations go with it; members stay in the community."""
    _require(user, "admin")
    event = session.get(Event, event_id)
    if event is None:
        raise CommunityError("That event no longer exists.")
    name = event.name
    session.delete(event)
    _audit(session, user, "events", event_id, "delete", {"Event": [name, None]})
    session.flush()
    return name


# ---------------------------------------------------------------- reading a Google Form export

# field → (label, header words that suggest it). Longer matches win, so "Team name" beats "Name".
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "name": ("Name", ("full name", "your name", "participant name", "name")),
    "email": ("Email", ("email address", "email id", "e mail", "email", "mail id")),
    "phone": ("Phone", ("phone number", "mobile number", "contact number", "phone", "mobile", "whatsapp")),
    "education": ("Education / profession", ("education profession", "education", "profession", "qualification",
                                              "degree", "course", "occupation", "current status", "year of study")),
    "institution": ("Organisation", ("institution", "college", "university", "organisation", "organization",
                                    "company", "institute", "school")),
    "city": ("City", ("city", "town")),
    "state": ("State", ("state",)),
    "interests": ("Interests", ("areas of interest", "interests", "interest", "domain")),
    "github_url": ("GitHub", ("github",)),
    "website_url": ("Website", ("website", "portfolio", "personal site")),
    "linkedin_url": ("LinkedIn", ("linkedin",)),
    "project_links": ("Project links", ("project links", "project link", "repository", "repo", "demo link",
                                        "submission", "project")),
    "team_name": ("Team name", ("team name", "team")),
    "role": ("Role", ("role",)),
    "consent": ("Consent to be contacted", ("consent", "i agree", "agree", "permission", "contact me",
                                            "receive updates")),
    "is_adult": ("18 or older", ("18 years", "18 or older", "above 18", "18", "age")),
}


def guess_mapping(columns: list[str]) -> dict[str, str | None]:
    """field → column, from the form's question wording. Each column is used once; best (longest) matches first."""
    candidates: list[tuple[int, str, str]] = []
    for column in columns:
        header = normalise(column)
        if header in ("timestamp", "score") or not header:
            continue
        for key, (_, words) in FIELDS.items():
            for word in words:
                if re.search(r"\b" + re.escape(normalise(word)) + r"\b", header):
                    candidates.append((len(normalise(word)) + (100 if header == normalise(word) else 0), key, column))
                    break
    mapping: dict[str, str | None] = {key: None for key in FIELDS}
    used: set[str] = set()
    for _, key, column in sorted(candidates, key=lambda c: -c[0]):
        if mapping[key] is None and column not in used:
            mapping[key] = column
            used.add(column)
    return mapping


def read_responses(data: bytes, file_name: str) -> pd.DataFrame:
    """A Google Forms response export (.csv) or its linked sheet (.xlsx). All cells come back as text."""
    from app.services.importing.readers import read_upload

    if len(data) > MAX_UPLOAD_BYTES:
        raise CommunityError("That file is larger than 15 MB.")
    if not file_name.lower().endswith((".csv", ".xlsx", ".xls")):
        raise CommunityError("Upload the responses as .csv or .xlsx (Google Forms → Responses → Download).")
    try:
        sheets = read_upload(data, file_name)
    except Exception as error:  # noqa: BLE001 — any parsing problem is reported plainly
        raise CommunityError(f"Couldn't read that file ({type(error).__name__}).") from error
    if not sheets or sheets[0].frame.empty:
        raise CommunityError("The file has no responses.")
    return sheets[0].frame.reset_index(drop=True)


_NO = ("no", "n", "false", "0", "disagree", "i disagree", "i do not agree", "do not agree", "not agree", "don t",
       "dont", "under 18", "below 18")


def parse_yes(value) -> bool | None:
    """Checkbox or Yes/No answers: any non-negative answer counts as yes. None = no answer."""
    text = _text(value)
    if text is None:
        return None
    norm = normalise(text)
    return not any(norm == n or norm.startswith(n + " ") for n in _NO)


def parse_adult(value) -> bool | None:
    text = _text(value)
    if text is None:
        return None
    number = re.fullmatch(r"\s*(\d{1,3})\s*(years?)?\s*", text.lower())
    if number:
        return int(number.group(1)) >= 18
    return parse_yes(text)


def clean_url(value, site: str | None = None) -> str | None:
    """Adds https:// when missing; a bare username becomes a GitHub/LinkedIn profile link."""
    text = _text(value, 500)
    if text is None:
        return None
    text = text.split()[0]
    if URL_RE.match(text):
        return text
    if text.startswith("www.") or "." in text.split("/")[0]:
        return "https://" + text
    if site == "github" and re.fullmatch(r"@?[A-Za-z0-9-]{1,39}", text):
        return f"https://github.com/{text.lstrip('@')}"
    if site == "linkedin" and re.fullmatch(r"[A-Za-z0-9-]{3,100}", text):
        return f"https://www.linkedin.com/in/{text}"
    return None


def _links(value) -> str | None:
    text = _text(value) if not isinstance(value, str) else value.strip()
    if not text:
        return None
    links = [clean_url(part) for part in re.split(r"[\s,;]+", text)]
    links = [link for link in links if link]
    return "\n".join(dict.fromkeys(links)) or None


def level_rules(session: Session) -> list[tuple[str, list[re.Pattern[str]]]]:
    rows = session.scalars(select(OptionList).where(OptionList.list_name == "community_level",
                                                    OptionList.is_active.is_(True)).order_by(OptionList.sort_order))
    return [(o.value, [re.compile(r"\b" + re.escape(normalise(a)) + r"\b") for a in (o.aliases or []) if normalise(a)])
            for o in rows]


def level_for(education: str | None, rules: list[tuple[str, list[re.Pattern[str]]]]) -> str | None:
    """'B.Tech ECE, 3rd year' → 'UG student', using the community_level list's words (Configuration)."""
    text = normalise(education)
    if not text:
        return None
    for value, patterns in rules:
        if any(p.search(text) for p in patterns):
            return value
    return "Other" if any(v == "Other" for v, _ in rules) else None


class InstitutionMatcher:
    """Exact matching of a typed organisation name against every organisation in the hub (name, short form, alias).
    Close spellings are only tried against organisations that are leads, where a wrong match is easy to spot."""

    def __init__(self, session: Session) -> None:
        self.exact: dict[str, int] = {}
        clash: set[str] = set()
        for oid, name, aliases in session.execute(select(Organisation.id, Organisation.name, Organisation.aliases)
                                                  .where(Organisation.org_type != "individual")):
            for variant in name_variants(name, aliases or []):
                if variant in self.exact and self.exact[variant] != oid:
                    clash.add(variant)
                self.exact.setdefault(variant, oid)
        for variant in clash:
            self.exact.pop(variant, None)  # the same name for two institutions: don't guess

    def find(self, text: str | None) -> int | None:
        if not text:
            return None
        for variant in name_variants(text):
            if variant in self.exact:
                return self.exact[variant]
        return None


@dataclass
class PreviewRow:
    row: int
    values: dict
    status: str          # New member · Existing member · Skipped
    note: str = ""


@dataclass
class Preview:
    rows: list[PreviewRow] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"Row": r.row, "Status": r.status, "Name": r.values.get("name") or "",
                              "Email": r.values.get("email") or "", "Organisation": r.values.get("institution") or "",
                              "Team": r.values.get("team_name") or "", "Level": r.values.get("level") or "",
                              "Mailable": bool(r.values.get("consent") and r.values.get("is_adult")),
                              "Note": r.note} for r in self.rows])


def _row_values(raw: dict, mapping: dict[str, str | None], rules) -> dict:
    def get(key):
        column = mapping.get(key)
        return raw.get(column) if column else None

    values = {
        "name": _text(get("name"), 160), "email": (_text(get("email"), 255) or "").lower() or None,
        "phone": _text(get("phone"), 40), "education": _text(get("education"), 255),
        "institution": _text(get("institution"), 255), "city": _text(get("city"), 80),
        "state": _text(get("state"), 60), "interests": _text(get("interests")),
        "github_url": clean_url(get("github_url"), "github"), "website_url": clean_url(get("website_url")),
        "linkedin_url": clean_url(get("linkedin_url"), "linkedin"), "project_links": _links(get("project_links")),
        "team_name": _text(get("team_name"), 160), "role": (_text(get("role")) or "").lower() or None,
        # A question that's on the form but left blank counts as "no"; a question that isn't on the form is unknown.
        "consent": bool(parse_yes(get("consent"))) if mapping.get("consent") else None,
        "is_adult": bool(parse_adult(get("is_adult"))) if mapping.get("is_adult") else None,
    }
    values["level"] = level_for(values["education"], rules)
    if values["role"] not in PARTICIPANT_ROLES:
        values["role"] = "participant"
    return values


def preview_import(session: Session, frame: pd.DataFrame, mapping: dict[str, str | None]) -> Preview:
    if not mapping.get("email"):
        raise CommunityError("Pick the column that holds the email address — it's how people are recognised.")
    rules = level_rules(session)
    existing = {e.lower() for e in session.scalars(select(CommunityMember.email))}
    suppressed, erased = set(), set()
    for hashed, reason in session.execute(select(Suppression.email_sha256, Suppression.reason)):
        suppressed.add(hashed)
        if (reason or "").startswith(ERASED_REASON):
            erased.add(hashed)
    preview, seen = Preview(), set()
    for i, raw in enumerate(frame.to_dict("records"), start=2):
        values = _row_values(raw, mapping, rules)
        email = values["email"]
        if not email or not EMAIL_RE.match(email):
            preview.rows.append(PreviewRow(i, values, "Skipped", "No valid email address"))
            continue
        if email in seen:
            preview.rows.append(PreviewRow(i, values, "Skipped", "Same email earlier in the file"))
            continue
        seen.add(email)
        if email_hash(email) in erased:
            preview.rows.append(PreviewRow(i, values, "Skipped", "Asked to be deleted — not stored again"))
            continue
        if email_hash(email) in suppressed:
            values["consent"] = False
        notes = []
        if not values["name"]:
            values["name"] = email.split("@")[0]
            notes.append("No name — using the email")
        if not (values["consent"] and values["is_adult"]):
            notes.append("Not mailable: " + ("on the do-not-contact list" if email_hash(email) in suppressed else
                                              "no consent" if not values["consent"] else "18+ not confirmed"))
        preview.rows.append(PreviewRow(i, values, "Existing member" if email in existing else "New member",
                                       "; ".join(notes)))
    return preview


def run_import(session: Session, preview: Preview, *, event_id: int, user: User, file_name: str,
               source: str | None = None) -> tuple[ImportBatch, dict[str, int]]:
    """Creates members (or fills blanks on existing ones) and their participation in the event. Undoable."""
    _require(user)
    event = session.get(Event, event_id)
    if event is None:
        raise CommunityError("Pick the event these responses belong to.")
    rows = [r for r in preview.rows if r.status != "Skipped"]
    if not rows:
        raise CommunityError("Nothing to import — every row was skipped.")
    matcher = InstitutionMatcher(session)
    batch = ImportBatch(kind="community", file_name=file_name[:255], sheet_name=event.name[:120],
                        imported_by_user_id=user.id, rows_total=len(preview.rows))
    session.add(batch)
    session.flush()
    stats = {"new members": 0, "updated members": 0, "added to event": 0, "already in event": 0,
             "matched organisations": 0}
    source = source or f"Google Form — {event.name}"
    for row in rows:
        v = row.values
        member = session.scalar(select(CommunityMember).where(func.lower(CommunityMember.email) == v["email"]))
        created = member is None
        if created:
            member = CommunityMember(name=v["name"], email=v["email"], source=source[:160])
            session.add(member)
        # Fill blanks only — never overwrite what's already known…
        for key in ("phone", "education", "level", "institution", "city", "state", "interests", "github_url",
                    "website_url", "linkedin_url"):
            if v.get(key) and not getattr(member, key):
                setattr(member, key, v[key])
        if v.get("project_links"):
            known = (member.project_links or "").splitlines()
            member.project_links = "\n".join(dict.fromkeys(known + v["project_links"].splitlines()))
        # …except consent and age: the latest answer is the one that counts (people can withdraw consent).
        if v.get("consent") is not None:
            member.consent = bool(v["consent"])
            member.consent_on = today_ist() if v["consent"] else None
        if v.get("is_adult") is not None:
            member.is_adult = bool(v["is_adult"]) or member.is_adult
        if member.org_id is None and member.institution:
            member.org_id = matcher.find(member.institution)
            stats["matched organisations"] += member.org_id is not None
        session.flush()
        if created:
            stats["new members"] += 1
            session.add(ImportBatchItem(batch_id=batch.id, entity="community_members", entity_id=member.id))
        else:
            stats["updated members"] += 1
        part = session.scalar(select(EventParticipant).where(EventParticipant.event_id == event.id,
                                                             EventParticipant.member_id == member.id))
        if part is None:
            first_link = (v.get("project_links") or "").splitlines()[:1]
            part = EventParticipant(event_id=event.id, member_id=member.id, team_name=v.get("team_name"),
                                    role=v.get("role") or "participant",
                                    project_link=first_link[0] if first_link else None)
            session.add(part)
            session.flush()
            session.add(ImportBatchItem(batch_id=batch.id, entity="event_participants", entity_id=part.id))
            stats["added to event"] += 1
        else:
            part.team_name = part.team_name or v.get("team_name")
            stats["already in event"] += 1
    batch.summary = [f"{k}: {n}" for k, n in stats.items()]
    _audit(session, user, "import_batches", batch.id, "create", {"Community import": [None, event.name]})
    session.flush()
    if event.start_on <= today_ist():
        sync_event_signals(session, event.id, user)
    return batch, stats


# ---------------------------------------------------------------- signals from events


def sync_event_signals(session: Session, event_id: int, user: User) -> int:
    """One 'Event participation' signal per organisation (that is a lead) per event, dated the event's start.
    Runs after an import of a past event and when an event is marked done. Safe to run again."""
    event = session.get(Event, event_id)
    kind = session.scalar(select(SignalType).where(SignalType.name == SIGNAL_NAME, SignalType.is_active.is_(True)))
    if event is None or kind is None or event.start_on > today_ist() or event.status == "cancelled":
        return 0
    org_ids = list(session.scalars(
        select(func.distinct(CommunityMember.org_id)).join(EventParticipant, EventParticipant.member_id ==
                                                           CommunityMember.id)
        .where(EventParticipant.event_id == event_id, CommunityMember.org_id.is_not(None),
               EventParticipant.status != "no_show",
               select(Lead.id).where(Lead.org_id == CommunityMember.org_id).exists())))
    added = 0
    for org_id in org_ids:
        exists = session.scalar(select(OrgSignal.id).where(OrgSignal.org_id == org_id,
                                                           OrgSignal.signal_type_id == kind.id,
                                                           OrgSignal.note == f"{event.kind}: {event.name}"[:200]))
        if exists:
            continue
        _, created = add_signal(session, org_id=org_id, type_id=kind.id, observed_on=event.start_on, user=user,
                                source_url=event.form_url, note=f"{event.kind}: {event.name}"[:200])
        added += created
    if added:
        recompute_scores(session, org_ids=org_ids)
    return added


# ---------------------------------------------------------------- participants


def participants_frame(session: Session, event_id: int) -> pd.DataFrame:
    rows = session.execute(
        select(EventParticipant, CommunityMember).join(CommunityMember, CommunityMember.id == EventParticipant.member_id)
        .where(EventParticipant.event_id == event_id)
        .order_by(func.coalesce(EventParticipant.team_name, "~"), CommunityMember.name)).all()
    return pd.DataFrame([{
        "id": p.id, "member_id": m.id, "Name": m.name, "Email": m.email, "Organisation": m.institution or "",
        "Level": m.level or "", "Team": p.team_name or "", "Role": ROLE_LABELS.get(p.role, p.role),
        "Status": STATUS_LABELS.get(p.status, p.status), "Project": p.project_title or "",
        "Project link": p.project_link or "", "GitHub": m.github_url or "",
        "Mailable": bool(m.consent and m.is_adult and not m.do_not_contact),
    } for p, m in rows], columns=["id", "member_id", "Name", "Email", "Organisation", "Level", "Team", "Role", "Status",
                                  "Project", "Project link", "GitHub", "Mailable"])


def save_participants(session: Session, event_id: int, frame: pd.DataFrame, user: User) -> int:
    """Saves team, role, result and project edits from the event's table."""
    _require(user)
    role_keys = {v: k for k, v in ROLE_LABELS.items()}
    status_keys = {v: k for k, v in STATUS_LABELS.items()}
    current = {p.id: p for p in session.scalars(select(EventParticipant).where(EventParticipant.event_id == event_id))}
    changed = 0
    for row in frame.to_dict("records"):
        part = current.get(int(row["id"]))
        if part is None:
            continue
        link = (row.get("Project link") or "").strip() or None
        if link and not URL_RE.match(link):
            link = clean_url(link)
        values = {"team_name": _text(row.get("Team"), 160), "role": role_keys.get(row.get("Role"), part.role),
                  "status": status_keys.get(row.get("Status"), part.status),
                  "project_title": _text(row.get("Project"), 255), "project_link": link}
        if any(getattr(part, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(part, k, v)
            changed += 1
    if changed:
        _audit(session, user, "events", event_id, "update", {"Participants": [None, f"{changed} change(s)"]})
        session.flush()
    return changed


def add_participant(session: Session, event_id: int, *, name: str, email: str, user: User,
                    institution: str | None = None, education: str | None = None, team_name: str | None = None,
                    role: str = "participant", consent: bool = False, is_adult: bool = False) -> EventParticipant:
    """Adds one person by hand (walk-ins, mentors, judges)."""
    _require(user)
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise CommunityError("Enter a valid email address.")
    if role not in PARTICIPANT_ROLES:
        raise CommunityError("Pick a role.")
    member = session.scalar(select(CommunityMember).where(func.lower(CommunityMember.email) == email))
    if member is None:
        member = CommunityMember(name=(name or email.split("@")[0]).strip()[:160], email=email,
                                 source="Added by hand", institution=_text(institution, 255),
                                 education=_text(education, 255), consent=consent, is_adult=is_adult,
                                 consent_on=today_ist() if consent else None)
        member.level = level_for(member.education, level_rules(session))
        member.org_id = InstitutionMatcher(session).find(member.institution)
        session.add(member)
        session.flush()
    if session.scalar(select(EventParticipant.id).where(EventParticipant.event_id == event_id,
                                                        EventParticipant.member_id == member.id)):
        raise CommunityError(f"{member.name} is already in this event.")
    part = EventParticipant(event_id=event_id, member_id=member.id, team_name=_text(team_name, 160), role=role)
    session.add(part)
    session.flush()
    return part


# ---------------------------------------------------------------- the community directory


def members_frame(session: Session) -> pd.DataFrame:
    events = {}
    for member_id, name, kind, status in session.execute(
            select(EventParticipant.member_id, Event.name, Event.kind, EventParticipant.status)
            .join(Event, Event.id == EventParticipant.event_id).order_by(Event.start_on)):
        events.setdefault(member_id, []).append((name, kind, status))
    leads = {oid for oid in session.scalars(select(func.distinct(Lead.org_id)))}
    org_names = dict(session.execute(select(Organisation.id, Organisation.name).where(
        Organisation.id.in_(select(CommunityMember.org_id).where(CommunityMember.org_id.is_not(None))))).all())
    records = []
    for m in session.scalars(select(CommunityMember).order_by(CommunityMember.name)):
        mine = events.get(m.id, [])
        records.append({
            "id": m.id, "Name": m.name, "Email": m.email, "Level": m.level or "", "Education": m.education or "",
            "Organisation": m.institution or "", "In the hub as": org_names.get(m.org_id, ""),
            "Organisation is a lead": m.org_id in leads if m.org_id else False, "org_id": m.org_id,
            "State": m.state or "", "Interests": m.interests or "", "Events": len(mine),
            "Event names": ", ".join(n for n, _, _ in mine), "Won": sum(1 for *_, s in mine if s == "winner"),
            "GitHub": m.github_url or "", "LinkedIn": m.linkedin_url or "", "Website": m.website_url or "",
            "Projects": m.project_links or "", "Mailable": bool(m.consent and m.is_adult and not m.do_not_contact),
            "Consent": m.consent, "18+": m.is_adult, "Do not contact": m.do_not_contact,
            # IST without a time zone: Excel can't store time-zone-aware dates.
            "Added": ist_date(m.created_at),
        })
    return pd.DataFrame(records, columns=[
        "id", "Name", "Email", "Level", "Education", "Organisation", "In the hub as", "Organisation is a lead",
        "org_id",
        "State", "Interests", "Events", "Event names", "Won", "GitHub", "LinkedIn", "Website", "Projects", "Mailable",
        "Consent", "18+", "Do not contact", "Added"])


def mailing_list(frame: pd.DataFrame) -> pd.DataFrame:
    """Only mailable members (consent + 18+ + not do-not-contact) — the rule applies to every export for outreach."""
    if frame.empty:
        return frame[["Name", "Email"]] if "Name" in frame else frame
    return frame.loc[frame["Mailable"], ["Name", "Email", "Organisation", "Level", "Interests"]].reset_index(drop=True)


def set_do_not_contact(session: Session, member_id: int, value: bool, user: User) -> None:
    _require(user)
    member = session.get(CommunityMember, member_id)
    if member is None:
        raise CommunityError("That person is no longer in the community.")
    member.do_not_contact = value
    if value:
        hashed = email_hash(member.email)
        if not session.scalar(select(Suppression.id).where(Suppression.email_sha256 == hashed)):
            session.add(Suppression(email_sha256=hashed, reason="Community: asked not to be contacted",
                                    added_by_user_id=user.id))
    _audit(session, user, "community_members", member_id, "update", {"Do not contact": [not value, value]})
    session.flush()


def delete_member(session: Session, member_id: int, user: User) -> str:
    """Admin only (DPDP deletion request): removes the person and their participations; their email goes on the
    do-not-contact list so a later import can't bring them back."""
    _require(user, "admin")
    member = session.get(CommunityMember, member_id)
    if member is None:
        raise CommunityError("That person is no longer in the community.")
    hashed = email_hash(member.email)
    existing = session.scalar(select(Suppression).where(Suppression.email_sha256 == hashed))
    if existing is None:
        session.add(Suppression(email_sha256=hashed, reason=ERASED_REASON, added_by_user_id=user.id))
    else:
        existing.reason = ERASED_REASON  # a deletion request is stronger than do-not-contact
    name = member.name
    session.delete(member)
    _audit(session, user, "community_members", member_id, "delete", {"Member": ["(deleted)", None]})
    session.flush()
    return name


def update_member(session: Session, member_id: int, values: dict, user: User) -> None:
    _require(user)
    member = session.get(CommunityMember, member_id)
    if member is None:
        raise CommunityError("That person is no longer in the community.")
    for key in ("name", "education", "institution", "city", "state", "interests", "notes"):
        if key in values:
            setattr(member, key, _text(values[key]) if key != "notes" else (values[key] or "").strip() or None)
    for key, site in (("github_url", "github"), ("linkedin_url", "linkedin"), ("website_url", None)):
        if key in values:
            setattr(member, key, clean_url(values[key], site))
    if "consent" in values:
        member.consent = bool(values["consent"])
        member.consent_on = today_ist() if member.consent else None
    if "is_adult" in values:
        member.is_adult = bool(values["is_adult"])
    if "institution" in values:
        member.org_id = InstitutionMatcher(session).find(member.institution)
    member.level = level_for(member.education, level_rules(session)) if "education" in values else member.level
    _audit(session, user, "community_members", member_id, "update", {"Member": [None, "edited"]})
    session.flush()


# ---------------------------------------------------------------- from community to sales


def create_lead_for_member(session: Session, member_id: int, track_id: int, user: User) -> tuple[Lead, str]:
    """Makes the member's organisation a lead in a track (or reuses the existing lead). Adds the organisation when
    it isn't in the hub yet, and backfills event-participation signals."""
    _require(user)
    member = session.get(CommunityMember, member_id)
    track = session.get(Track, track_id)
    if member is None or track is None:
        raise CommunityError("Pick a member and a track.")
    org = session.get(Organisation, member.org_id) if member.org_id else None
    note = ""
    if org is None:
        name = (member.institution or "").strip()
        if not name:
            raise CommunityError("This person has no organisation — add it on their profile first.")
        kind = guess_org_type(name)
        org = Organisation(name=name[:255], org_type=kind, industry=default_industry(kind), state=member.state,
                           city=member.city,
                           notes=f"Added from the community ({member.name}).")
        session.add(org)
        session.flush()
        member.org_id = org.id
        note = f"New organisation added: {name}. "
    lead = session.scalar(select(Lead).where(Lead.org_id == org.id, Lead.track_id == track.id))
    if lead is not None:
        return lead, note + f"{org.name} is already a lead in {track.name}."
    first = session.scalar(select(Stage).where(Stage.outcome == "open", Stage.is_active.is_(True))
                           .order_by(Stage.sort_order).limit(1))
    lead = Lead(org_id=org.id, track_id=track.id, stage=first, source="Community / events",
                owner_user_id=user.id, notes=f"From the community: {member.name} ({member.email}).")
    session.add(lead)
    session.flush()
    _audit(session, user, "leads", lead.id, "create", {"Lead": [None, f"{org.name} from the community"]})
    for event_id in session.scalars(select(EventParticipant.event_id).where(EventParticipant.member_id == member.id)):
        sync_event_signals(session, event_id, user)
    return lead, note + f"Lead created: {org.name} in {track.name}."


# ---------------------------------------------------------------- for the weekly report


def community_week(session: Session, first_day: date, last_day: date) -> dict:
    from app.services.report_metrics import week_range

    start, end = week_range(first_day)
    held = session.execute(select(Event.name, Event.kind, Event.start_on).where(
        Event.start_on >= first_day, Event.start_on <= last_day, Event.status != "cancelled")
        .order_by(Event.start_on)).all()
    new_members = int(session.scalar(select(func.count(CommunityMember.id)).where(
        CommunityMember.created_at >= start, CommunityMember.created_at < end)) or 0)
    total = int(session.scalar(select(func.count(CommunityMember.id))) or 0)
    mailable = int(session.scalar(select(func.count(CommunityMember.id)).where(
        CommunityMember.consent.is_(True), CommunityMember.is_adult.is_(True),
        CommunityMember.do_not_contact.is_(False))) or 0)
    top = session.execute(
        select(func.coalesce(Organisation.name, CommunityMember.institution), func.count(EventParticipant.id))
        .join(CommunityMember, CommunityMember.id == EventParticipant.member_id)
        .join(Event, Event.id == EventParticipant.event_id)
        .outerjoin(Organisation, Organisation.id == CommunityMember.org_id)
        .where(Event.start_on >= first_day, Event.start_on <= last_day)
        .group_by(func.coalesce(Organisation.name, CommunityMember.institution))
        .order_by(func.count(EventParticipant.id).desc()).limit(5)).all()
    return {"events": [{"Event": n, "Type": k, "Date": d.isoformat()} for n, k, d in held],
            "new_members": new_members, "members": total, "mailable": mailable,
            "top_institutions": [{"Organisation": i or "Not given", "Participants": int(c)} for i, c in top]}
