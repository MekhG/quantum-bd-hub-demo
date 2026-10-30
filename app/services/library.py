"""Knowledge base: documents (links), scripts with fill-in fields, objections, versions, approval, content check.

Rules:
- Members add and edit drafts; only an admin approves (so nobody sends an outdated brochure) or archives.
- A document or script keeps its versions in one family. The *current* version is the latest approved one,
  or the latest draft while nothing in the family is approved yet.
- Before approval, text is checked against the flagged-words list (Configuration → Content check).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    AuditLog,
    CadenceStep,
    Contact,
    Document,
    Lead,
    Objection,
    OptionList,
    Organisation,
    Product,
    Script,
    Track,
    User,
    activity_documents,
)
from app.services.cadence import today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
URL_RE = re.compile(r"^https?://[^\s<>\"']+\.[^\s<>\"']+$", re.IGNORECASE)
FIELD_RE = re.compile(r"\{([a-z_]+)\}")
TITLES = {"dr", "prof", "professor", "mr", "mrs", "ms", "miss", "shri", "smt", "sri"}

# Fill-in fields a script can use, with what each becomes. Shown on the Scripts tab.
MERGE_FIELDS: dict[str, str] = {
    "salutation": "Dr / Prof. from their name, if any",
    "first_name": "First name (titles removed)",
    "last_name": "Last name",
    "name": "Full name as saved",
    "designation": "Designation",
    "department": "Department",
    "organisation": "Organisation name",
    "institution": "Organisation name (older scripts)",
    "industry": "Organisation's industry",
    "state": "Organisation's state",
    "product": "Lead's product fit (first product), or 'our quantum lab platforms'",
    "sender_name": "Lead owner's name",
    "sender_email": "The track's sender mailbox",
}


class LibraryError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _audit(session: Session, user: User, entity: str, entity_id: int, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity=entity, entity_id=str(entity_id), action=action, diff=diff))


# ---------------------------------------------------------------- content check


def content_flags(session: Session, *texts: str | None) -> list[tuple[str, str]]:
    """Flagged words found in the texts, as (word, suggested wording). Case-insensitive, whole words only."""
    joined = "\n".join(t for t in texts if t)
    if not joined:
        return []
    found: list[tuple[str, str]] = []
    for word, aliases in session.execute(
            select(OptionList.value, OptionList.aliases)
            .where(OptionList.list_name == "flagged_words", OptionList.is_active.is_(True))
            .order_by(OptionList.sort_order)):
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", joined, re.IGNORECASE):
            found.append((word, (aliases or [""])[0] if aliases else ""))
    return found


def flags_message(flags: list[tuple[str, str]]) -> str:
    return "; ".join(f"“{w}”" + (f" → use “{s}”" if s else "") for w, s in flags)


# ---------------------------------------------------------------- versions (shared by documents and scripts)


def _family(session: Session, model, family_id: int) -> list[Any]:
    return list(session.scalars(select(model).where(model.family_id == family_id).order_by(model.version_no)))


def _refresh_current(session: Session, model, family_id: int) -> None:
    """Current = latest approved version; if none is approved, the latest draft."""
    rows = _family(session, model, family_id)
    approved = [r for r in rows if r.status == "approved"]
    drafts = [r for r in rows if r.status == "draft"]
    current = approved[-1] if approved else (drafts[-1] if drafts else None)
    for r in rows:
        r.is_current = r is current


def _approve(session: Session, model, row, user: User, *, texts: Iterable[str | None], override: bool) -> None:
    _require(user, "admin")
    if row.status == "archived":
        raise LibraryError("Archived items can't be approved — make a new version instead.")
    flags = content_flags(session, *texts)
    if flags and not override:
        raise LibraryError("Check the wording first: " + flags_message(flags))
    row.status, row.approved_on = "approved", today_ist()
    row.approved_by_user_id = user.id
    session.flush()
    _refresh_current(session, model, row.family_id)
    _audit(session, user, model.__tablename__, row.id, "update",
           {"Status": ["draft", "approved"]} | ({"Flagged words kept": [None, flags_message(flags)]} if flags else {}))
    session.flush()


def _archive(session: Session, model, row, user: User) -> None:
    _require(user, "admin")
    old = row.status
    row.status = "archived"
    session.flush()
    _refresh_current(session, model, row.family_id)
    _audit(session, user, model.__tablename__, row.id, "update", {"Status": [old, "archived"]})
    session.flush()


# ---------------------------------------------------------------- documents


@dataclass
class DocumentData:
    title: str
    doc_type: str
    url: str
    description: str | None = None
    key_text: str | None = None
    tags: str | None = None
    version_label: str | None = None
    product_ids: list[int] = field(default_factory=list)
    track_ids: list[int] = field(default_factory=list)


def _clean_document(session: Session, data: DocumentData) -> DocumentData:
    title = (data.title or "").strip()
    url = (data.url or "").strip()
    if not title:
        raise LibraryError("Give the document a title.")
    if not URL_RE.fullmatch(url):
        raise LibraryError("Paste a full link starting with https:// (e.g. a Google Drive share link).")
    if not data.doc_type:
        raise LibraryError("Pick a document type.")
    tags = ", ".join(dict.fromkeys(t.strip() for t in (data.tags or "").split(",") if t.strip())) or None
    return DocumentData(title=title[:200], doc_type=data.doc_type, url=url[:1000],
                        description=(data.description or "").strip() or None,
                        key_text=(data.key_text or "").strip() or None, tags=tags,
                        version_label=(data.version_label or "").strip()[:40] or None,
                        product_ids=list(data.product_ids), track_ids=list(data.track_ids))


def _apply_document(session: Session, doc: Document, data: DocumentData) -> None:
    doc.title, doc.doc_type, doc.url = data.title, data.doc_type, data.url
    doc.description, doc.key_text, doc.tags, doc.version_label = (data.description, data.key_text, data.tags,
                                                                    data.version_label)
    doc.products = list(session.scalars(select(Product).where(Product.id.in_(data.product_ids)))) \
        if data.product_ids else []
    doc.tracks = list(session.scalars(select(Track).where(Track.id.in_(data.track_ids)))) if data.track_ids else []


def add_document(session: Session, data: DocumentData, user: User) -> tuple[Document, list[tuple[str, str]]]:
    """Saves a new document as a draft. Returns it plus any flagged words (shown as a warning)."""
    _require(user)
    data = _clean_document(session, data)
    duplicate = session.scalar(select(Document.id).where(Document.url == data.url, Document.status != "archived"))
    if duplicate:
        raise LibraryError("That link is already in the Library — open it and add a new version instead.")
    doc = Document(owner_user_id=user.id, status="draft", version_no=1, is_current=True)
    _apply_document(session, doc, data)
    session.add(doc)
    session.flush()
    doc.family_id = doc.id
    _audit(session, user, "documents", doc.id, "create", {"Title": [None, doc.title]})
    session.flush()
    return doc, content_flags(session, data.title, data.description, data.key_text)


def update_document(session: Session, doc_id: int, data: DocumentData, user: User) -> list[tuple[str, str]]:
    """Edits a document in place. Members can edit drafts; an approved document needs an admin
    (or a new version), so what the team is sending never changes silently."""
    _require(user)
    doc = session.get(Document, doc_id)
    if doc is None:
        raise LibraryError("That document no longer exists.")
    if doc.status != "draft" and _ROLE_RANK.get(user.role, -1) < 2:
        raise LibraryError("Approved documents can only be changed by an admin — add a new version instead.")
    data = _clean_document(session, data)
    before = {"Title": doc.title, "Link": doc.url}
    _apply_document(session, doc, data)
    _audit(session, user, "documents", doc.id, "update",
           {k: [v, {"Title": doc.title, "Link": doc.url}[k]] for k, v in before.items()
            if v != {"Title": doc.title, "Link": doc.url}[k]})
    session.flush()
    return content_flags(session, data.title, data.description, data.key_text)


def new_document_version(session: Session, doc_id: int, data: DocumentData,
                         user: User) -> tuple[Document, list[tuple[str, str]]]:
    """A newer version of the same document (new link). Starts as a draft; the approved one stays current until an
    admin approves the new one."""
    _require(user)
    old = session.get(Document, doc_id)
    if old is None:
        raise LibraryError("That document no longer exists.")
    data = _clean_document(session, data)
    latest = max(r.version_no for r in _family(session, Document, old.family_id))
    doc = Document(owner_user_id=user.id, status="draft", family_id=old.family_id, version_no=latest + 1,
                   is_current=False)
    _apply_document(session, doc, data)
    session.add(doc)
    session.flush()
    _refresh_current(session, Document, old.family_id)
    _audit(session, user, "documents", doc.id, "create", {"Version": [None, f"v{doc.version_no} of {doc.title}"]})
    session.flush()
    return doc, content_flags(session, data.title, data.description, data.key_text)


def approve_document(session: Session, doc_id: int, user: User, *, override_flags: bool = False) -> None:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise LibraryError("That document no longer exists.")
    _approve(session, Document, doc, user, texts=(doc.title, doc.description, doc.key_text), override=override_flags)


def archive_document(session: Session, doc_id: int, user: User) -> None:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise LibraryError("That document no longer exists.")
    _archive(session, Document, doc, user)


def _words(text: str | None) -> list[str]:
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def documents_frame(session: Session, *, text: str | None = None, include_old: bool = False) -> pd.DataFrame:
    """Library list. Every word typed must appear somewhere: title, description, key text, tags or type."""
    query = (select(Document).options(selectinload(Document.products), selectinload(Document.tracks),
                                      selectinload(Document.owner))
             .order_by(Document.status != "approved", Document.title, Document.version_no.desc()))
    if not include_old:
        query = query.where(Document.is_current.is_(True), Document.status != "archived")
    for word in _words(text):
        pattern = _like(word)
        query = query.where(or_(*(column.ilike(pattern, escape="\\") for column in (
            Document.title, Document.description, Document.key_text, Document.tags, Document.doc_type))))
    docs = list(session.scalars(query))
    shared = dict(session.execute(
        select(activity_documents.c.document_id, func.count()).group_by(activity_documents.c.document_id)).all())
    return pd.DataFrame([{
        "id": d.id, "family_id": d.family_id, "Title": d.title, "Type": d.doc_type, "Status": d.status.title(),
        "Version": f"v{d.version_no}" + (f" · {d.version_label}" if d.version_label else ""),
        "Current": d.is_current, "Products": ", ".join(p.name for p in d.products),
        "Tracks": ", ".join(t.name for t in d.tracks), "Tags": d.tags or "", "Link": d.url,
        "Owner": d.owner.name if d.owner else "", "Updated": d.updated_at, "Shared": int(shared.get(d.id, 0)),
    } for d in docs], columns=["id", "family_id", "Title", "Type", "Status", "Version", "Current", "Products",
                               "Tracks", "Tags", "Link", "Owner", "Updated", "Shared"])


def document_versions(session: Session, family_id: int) -> list[Document]:
    return list(session.scalars(select(Document).where(Document.family_id == family_id)
                                .options(selectinload(Document.owner), selectinload(Document.approved_by))
                                .order_by(Document.version_no.desc())))


def shareable_documents(session: Session) -> list[tuple[int, str]]:
    """Current approved documents, for the 'Documents shared' picker when logging an activity."""
    return [(d.id, f"{d.title} · {d.doc_type}") for d in session.scalars(
        select(Document).where(Document.is_current.is_(True), Document.status == "approved")
        .order_by(Document.title))]


def attach_documents(session: Session, activity: Activity, document_ids: Iterable[int]) -> int:
    ids = [int(i) for i in document_ids]
    if not ids:
        return 0
    already = set(session.scalars(select(activity_documents.c.document_id)
                                  .where(activity_documents.c.activity_id == activity.id)))
    new = [i for i in ids if i not in already]
    for doc_id in new:
        session.execute(activity_documents.insert().values(activity_id=activity.id, document_id=doc_id))
    session.flush()
    return len(new)


def documents_for_org(session: Session, org_id: int) -> pd.DataFrame:
    """What was shared with this organisation, newest first (the lead record's Documents tab)."""
    rows = session.execute(
        select(func.coalesce(Activity.occurred_at, Activity.scheduled_at), Document.title, Document.doc_type,
               Document.url, Document.version_no, Activity.title, User.name)
        .join(activity_documents, activity_documents.c.activity_id == Activity.id)
        .join(Document, Document.id == activity_documents.c.document_id)
        .outerjoin(User, User.id == Activity.owner_user_id)
        .where(Activity.org_id == org_id)
        .order_by(func.coalesce(Activity.occurred_at, Activity.scheduled_at).desc())
    ).all()
    return pd.DataFrame([{"When": w, "Document": f"{t} (v{v})", "Type": k, "Link": u, "Shared in": a, "By": b or ""}
                         for w, t, k, u, v, a, b in rows],
                        columns=["When", "Document", "Type", "Link", "Shared in", "By"])


# ---------------------------------------------------------------- scripts


@dataclass
class ScriptData:
    title: str
    kind: str
    body: str
    subject: str | None = None
    track_id: int | None = None
    cadence_step_id: int | None = None
    notes: str | None = None


def _clean_script(session: Session, data: ScriptData) -> ScriptData:
    title, body = (data.title or "").strip(), (data.body or "").strip()
    if not title or not body:
        raise LibraryError("A script needs a title and its text.")
    unknown = sorted(set(FIELD_RE.findall(f"{data.subject or ''}\n{body}")) - set(MERGE_FIELDS))
    if unknown:
        raise LibraryError("Unknown fill-in field(s): " + ", ".join("{" + u + "}" for u in unknown)
                           + ". Use the fields listed on the Scripts tab.")
    if data.cadence_step_id:
        step = session.get(CadenceStep, data.cadence_step_id)
        if step is None or (data.track_id and step.track_id != data.track_id):
            raise LibraryError("That cadence step belongs to a different track.")
    return ScriptData(title=title[:200], kind=data.kind, body=body,
                      subject=(data.subject or "").strip()[:300] or None, track_id=data.track_id,
                      cadence_step_id=data.cadence_step_id, notes=(data.notes or "").strip() or None)


def _apply_script(script: Script, data: ScriptData) -> None:
    script.title, script.kind, script.body, script.subject = data.title, data.kind, data.body, data.subject
    script.track_id, script.cadence_step_id, script.notes = data.track_id, data.cadence_step_id, data.notes


def add_script(session: Session, data: ScriptData, user: User) -> tuple[Script, list[tuple[str, str]]]:
    _require(user)
    data = _clean_script(session, data)
    script = Script(owner_user_id=user.id, status="draft", version_no=1, is_current=True)
    _apply_script(script, data)
    session.add(script)
    session.flush()
    script.family_id = script.id
    _audit(session, user, "scripts", script.id, "create", {"Title": [None, script.title]})
    session.flush()
    return script, content_flags(session, data.subject, data.body)


def update_script(session: Session, script_id: int, data: ScriptData, user: User) -> list[tuple[str, str]]:
    _require(user)
    script = session.get(Script, script_id)
    if script is None:
        raise LibraryError("That script no longer exists.")
    if script.status != "draft" and _ROLE_RANK.get(user.role, -1) < 2:
        raise LibraryError("Approved scripts can only be changed by an admin — save a new version instead.")
    data = _clean_script(session, data)
    _apply_script(script, data)
    _audit(session, user, "scripts", script.id, "update", {"Text": ["…", "edited"]})
    session.flush()
    return content_flags(session, data.subject, data.body)


def new_script_version(session: Session, script_id: int, data: ScriptData,
                       user: User) -> tuple[Script, list[tuple[str, str]]]:
    _require(user)
    old = session.get(Script, script_id)
    if old is None:
        raise LibraryError("That script no longer exists.")
    data = _clean_script(session, data)
    latest = max(r.version_no for r in _family(session, Script, old.family_id))
    script = Script(owner_user_id=user.id, status="draft", family_id=old.family_id, version_no=latest + 1,
                    is_current=False)
    _apply_script(script, data)
    session.add(script)
    session.flush()
    _refresh_current(session, Script, old.family_id)
    _audit(session, user, "scripts", script.id, "create", {"Version": [None, f"v{script.version_no}"]})
    session.flush()
    return script, content_flags(session, data.subject, data.body)


def approve_script(session: Session, script_id: int, user: User, *, override_flags: bool = False) -> None:
    script = session.get(Script, script_id)
    if script is None:
        raise LibraryError("That script no longer exists.")
    _approve(session, Script, script, user, texts=(script.subject, script.body), override=override_flags)


def archive_script(session: Session, script_id: int, user: User) -> None:
    script = session.get(Script, script_id)
    if script is None:
        raise LibraryError("That script no longer exists.")
    _archive(session, Script, script, user)


def scripts_frame(session: Session, *, text: str | None = None, include_old: bool = False) -> pd.DataFrame:
    step = CadenceStep
    query = (select(Script, Track.name, step.step_no, step.name)
             .outerjoin(Track, Track.id == Script.track_id)
             .outerjoin(step, step.id == Script.cadence_step_id)
             .order_by(Track.sort_order, step.step_no, Script.title, Script.version_no.desc()))
    if not include_old:
        query = query.where(Script.is_current.is_(True), Script.status != "archived")
    for word in _words(text):
        pattern = _like(word)
        query = query.where(or_(Script.title.ilike(pattern, escape="\\"), Script.subject.ilike(pattern, escape="\\"),
                                Script.body.ilike(pattern, escape="\\")))
    return pd.DataFrame([{
        "id": s.id, "family_id": s.family_id, "Title": s.title, "Kind": s.kind.title(), "Track": track or "Any",
        "Cadence step": f"{no}. {name}" if no else "", "Status": s.status.title(), "Version": f"v{s.version_no}",
        "Subject": s.subject or "", "Updated": s.updated_at,
    } for s, track, no, name in session.execute(query)],
        columns=["id", "family_id", "Title", "Kind", "Track", "Cadence step", "Status", "Version", "Subject",
                 "Updated"])


def script_versions(session: Session, family_id: int) -> list[Script]:
    return list(session.scalars(select(Script).where(Script.family_id == family_id)
                                .order_by(Script.version_no.desc())))


def scripts_for_step(session: Session) -> dict[int, Script]:
    """cadence_step_id → its current approved email script (what the email batch fills in)."""
    out: dict[int, Script] = {}
    for script in session.scalars(select(Script).where(Script.is_current.is_(True), Script.status == "approved",
                                                       Script.cadence_step_id.is_not(None))
                                  .order_by(Script.updated_at)):
        out[script.cadence_step_id] = script
    return out


def usable_scripts(session: Session, kind: str | None = None) -> list[tuple[int, str]]:
    """Approved current scripts for the 'Script used' picker."""
    query = select(Script).where(Script.is_current.is_(True), Script.status == "approved").order_by(Script.title)
    if kind:
        query = query.where(Script.kind == kind)
    return [(s.id, s.title) for s in session.scalars(query)]


def split_name(name: str | None) -> tuple[str, str, str]:
    """'Dr. Ananya Rao' → ('Dr', 'Ananya', 'Rao'). Titles are recognised with or without a full stop."""
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    salutation = ""
    while words and words[0].rstrip(".").lower() in TITLES:
        word = words.pop(0).rstrip(".")
        if not salutation and word.lower() in {"dr", "prof", "professor"}:
            salutation = "Dr" if word.lower() == "dr" else "Prof."
    first = words[0] if words else ""
    last = words[-1] if len(words) > 1 else ""
    return salutation, first, last


def merge_values(*, contact: Contact | None, org: Organisation | None, lead: Lead | None,
                 sender_name: str | None = None, sender_email: str | None = None) -> dict[str, str]:
    salutation, first, last = split_name(contact.name if contact else "")
    products = [p.name for p in (lead.product_fit if lead is not None else [])]
    return {
        "salutation": salutation, "first_name": first, "last_name": last or first,
        "name": contact.name if contact else "", "designation": (contact.designation or "") if contact else "",
        "department": (contact.department or "") if contact else "",
        "institution": org.name if org else "", "organisation": org.name if org else "",
        "industry": (org.industry or "") if org else "", "state": (org.state or "") if org else "",
        "product": products[0] if products else "our quantum lab platforms",
        "sender_name": sender_name or "Team Demo", "sender_email": sender_email or "",
    }


def render(text: str | None, values: dict[str, str]) -> tuple[str, list[str]]:
    """Replaces {field} with its value. Returns the text and the fields that came out empty (worth a check)."""
    empty: list[str] = []

    def swap(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        if not values[name]:
            empty.append(name)
        return values[name]

    out = FIELD_RE.sub(swap, text or "")
    # Tidy what an empty field leaves behind, e.g. "Dear  Rao," or "Dear ,".
    out = re.sub(r"[ \t]{2,}", " ", out).replace(" ,", ",")
    return out, sorted(set(empty))


# ---------------------------------------------------------------- objections


@dataclass
class ObjectionData:
    objection: str
    response: str
    tags: str | None = None
    product_ids: list[int] = field(default_factory=list)
    track_ids: list[int] = field(default_factory=list)


def save_objection(session: Session, data: ObjectionData, user: User,
                   objection_id: int | None = None) -> tuple[Objection, list[tuple[str, str]]]:
    _require(user)
    said, answer = (data.objection or "").strip(), (data.response or "").strip()
    if not said or not answer:
        raise LibraryError("Fill in both what they say and what we say.")
    if objection_id:
        row = session.get(Objection, objection_id)
        if row is None:
            raise LibraryError("That entry no longer exists.")
        if row.status == "approved" and _ROLE_RANK.get(user.role, -1) < 2:
            raise LibraryError("Approved answers can only be changed by an admin.")
    else:
        row = Objection(owner_user_id=user.id, status="draft")
        session.add(row)
    row.objection, row.response = said, answer
    row.tags = ", ".join(dict.fromkeys(t.strip() for t in (data.tags or "").split(",") if t.strip())) or None
    row.products = list(session.scalars(select(Product).where(Product.id.in_(data.product_ids)))) \
        if data.product_ids else []
    row.tracks = list(session.scalars(select(Track).where(Track.id.in_(data.track_ids)))) if data.track_ids else []
    session.flush()
    _audit(session, user, "objections", row.id, "update" if objection_id else "create", {"Objection": [None, said[:80]]})
    return row, content_flags(session, said, answer)


def set_objection_status(session: Session, objection_id: int, status: str, user: User, *,
                         override_flags: bool = False) -> None:
    _require(user, "admin")
    row = session.get(Objection, objection_id)
    if row is None:
        raise LibraryError("That entry no longer exists.")
    if status == "approved":
        flags = content_flags(session, row.objection, row.response)
        if flags and not override_flags:
            raise LibraryError("Check the wording first: " + flags_message(flags))
    old, row.status = row.status, status
    _audit(session, user, "objections", row.id, "update", {"Status": [old, status]})
    session.flush()


def objections_frame(session: Session, *, text: str | None = None, product_id: int | None = None,
                     track_id: int | None = None, include_archived: bool = False) -> pd.DataFrame:
    query = (select(Objection).options(selectinload(Objection.products), selectinload(Objection.tracks))
             .order_by(Objection.is_pinned.desc(), Objection.status != "approved", Objection.times_used.desc(),
                       Objection.id))
    if not include_archived:
        query = query.where(Objection.status != "archived")
    for word in _words(text):
        pattern = _like(word)
        query = query.where(or_(Objection.objection.ilike(pattern, escape="\\"),
                                Objection.response.ilike(pattern, escape="\\"),
                                Objection.tags.ilike(pattern, escape="\\")))
    rows = list(session.scalars(query))
    if product_id:
        rows = [r for r in rows if not r.products or any(p.id == product_id for p in r.products)]
    if track_id:
        rows = [r for r in rows if not r.tracks or any(t.id == track_id for t in r.tracks)]
    return pd.DataFrame([{"id": r.id, "They say": r.objection, "We say": r.response, "Tags": r.tags or "",
                          "Products": ", ".join(p.name for p in r.products),
                          "Tracks": ", ".join(t.name for t in r.tracks), "Status": r.status.title(),
                          "product_ids": [p.id for p in r.products], "track_ids": [t.id for t in r.tracks]}
                         for r in rows],
                        columns=["id", "They say", "We say", "Tags", "Products", "Tracks", "Status", "product_ids",
                                 "track_ids"])


def mark_objection_used(session: Session, objection_id: int) -> None:
    row = session.get(Objection, objection_id)
    if row is not None:
        row.times_used = (row.times_used or 0) + 1
        session.flush()


# ---------------------------------------------------------------- search (top bar)


def search_library(session: Session, text: str, limit: int = 5) -> list[tuple[str, str, str]]:
    """(kind, title, link-or-detail) for the global search: current documents and scripts."""
    words = _words(text)
    if not words or len(text.strip()) < 2:
        return []
    doc_q = select(Document).where(Document.is_current.is_(True), Document.status != "archived")
    script_q = select(Script).where(Script.is_current.is_(True), Script.status != "archived")
    for word in words:
        pattern = _like(word)
        doc_q = doc_q.where(or_(Document.title.ilike(pattern, escape="\\"), Document.tags.ilike(pattern, escape="\\"),
                                Document.key_text.ilike(pattern, escape="\\")))
        script_q = script_q.where(or_(Script.title.ilike(pattern, escape="\\"),
                                      Script.subject.ilike(pattern, escape="\\")))
    hits = [("document", d.title, d.url) for d in session.scalars(doc_q.order_by(Document.title).limit(limit))]
    hits += [("script", s.title, s.kind) for s in session.scalars(script_q.order_by(Script.title).limit(limit))]
    return hits


__all__ = [
    "MERGE_FIELDS", "DocumentData", "LibraryError", "ObjectionData", "ScriptData", "add_document", "add_script",
    "approve_document", "approve_script", "archive_document", "archive_script", "attach_documents",
    "content_flags", "document_versions", "documents_for_org", "documents_frame", "flags_message", "merge_values",
    "new_document_version", "new_script_version", "objections_frame", "render", "save_objection",
    "script_versions", "scripts_for_step", "scripts_frame", "search_library", "set_objection_status",
    "shareable_documents", "split_name", "update_document", "update_script", "usable_scripts",
]


# ---------------------------------------------------------------- content check list (Configuration)


def flagged_frame(session: Session) -> pd.DataFrame:
    return pd.DataFrame([{"id": o.id, "Word or phrase": o.value, "Use instead": (o.aliases or [""])[0] if o.aliases
                          else "", "Active": o.is_active}
                         for o in session.scalars(select(OptionList).where(OptionList.list_name == "flagged_words")
                                                  .order_by(OptionList.sort_order, OptionList.id))],
                        columns=["id", "Word or phrase", "Use instead", "Active"])


def save_flagged(session: Session, frame: pd.DataFrame, user: User) -> int:
    """Admin edits the flagged-words list. Untick Active to stop checking a word (rows are kept for the record)."""
    _require(user, "admin")
    existing = {o.id: o for o in session.scalars(select(OptionList).where(OptionList.list_name == "flagged_words"))}
    taken = {o.value.lower(): o.id for o in existing.values()}
    changed = 0
    for order, rec in enumerate(frame.to_dict("records"), start=1):
        word = str(rec.get("Word or phrase") or "").strip()
        if not word:
            continue
        instead = str(rec.get("Use instead") or "").strip()
        rid = rec.get("id")
        row = existing.get(int(rid)) if rid is not None and not pd.isna(rid) else None
        if row is None and word.lower() in taken:
            row = existing[taken[word.lower()]]
        values = dict(value=word[:80], aliases=[instead] if instead else None, is_active=rec.get("Active") is not False,
                      sort_order=order)
        if row is None:
            session.add(OptionList(list_name="flagged_words", **values))
            taken[word.lower()] = -1
            changed += 1
        elif any(getattr(row, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(row, k, v)
            changed += 1
    if changed:
        _audit(session, user, "option_lists", "flagged_words", "update", {"Flagged words": [None, f"{changed} change(s)"]})
    session.flush()
    return changed


# ---------------------------------------------------------------- removing links


def document_share_count(session: Session, doc_id: int) -> int:
    return int(session.scalar(select(func.count()).select_from(activity_documents)
                              .where(activity_documents.c.document_id == doc_id)) or 0)


def can_delete_document(doc: Document, user: User) -> bool:
    """Admins can remove any link; members can remove their own drafts."""
    if _ROLE_RANK.get(user.role, -1) >= 2:
        return True
    return _ROLE_RANK.get(user.role, -1) >= 1 and doc.status == "draft" and doc.owner_user_id == user.id


def delete_document(session: Session, doc_id: int, user: User) -> str:
    """Removes a link from the Library for good. A link that was already shared with a lead is kept, so the lead's
    history still shows what was sent — archive it instead (admin)."""
    _require(user)
    doc = session.get(Document, doc_id)
    if doc is None:
        raise LibraryError("That document no longer exists.")
    if not can_delete_document(doc, user):
        raise LibraryError("You can remove your own drafts. Approved documents are archived by an admin instead.")
    shared = document_share_count(session, doc_id)
    if shared:
        raise LibraryError(f"This version was shared with leads {shared} time(s), so it stays for their history. "
                           "Archive it instead — it disappears from the Library but the record is kept.")
    family, title = doc.family_id, doc.title
    _audit(session, user, "documents", doc.id, "delete", {"Title": [title, None], "Link": [doc.url, None]})
    session.delete(doc)
    session.flush()
    if family is not None:
        remaining = _family(session, Document, family)
        if remaining and all(r.id != family for r in remaining):
            # The family is named after its first version; re-point it so versions stay grouped.
            for r in remaining:
                r.family_id = remaining[0].id
            family = remaining[0].id
        if remaining:
            _refresh_current(session, Document, family)
    session.flush()
    return f"“{title}” removed from the Library."
