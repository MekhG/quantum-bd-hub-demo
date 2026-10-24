"""Saving edits made in the Leads, Colleges and Contacts tables.

Flow: the page shows a table → the person edits cells (or, as admin, deletes rows) → `plan_edits` lists
what changed → the person confirms → `apply_plan` checks and saves it.

Safety rules:
- Every value is validated before anything is written; a row with a problem is skipped as a whole.
- A row someone else changed since this table was loaded is skipped and reported (no silent overwrite).
- Every change is written to the audit log with old → new values.
- Stage changes are also written to the lead's timeline as a 'Stage change' activity.
- Only admins can delete; institutions can only be deleted once they have no leads or contacts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    ActivityType,
    AuditLog,
    ConsentLog,
    Contact,
    Lead,
    LostReason,
    OptionList,
    Organisation,
    Product,
    Stage,
    Suppression,
    User,
)
from app.services.importing.normalise import (
    EMAIL_RE,
    INDIAN_STATES,
    domain_from_website,
    match_state,
    org_type_from_aishe,
    parse_phones,
)
from app.services.cadence import stop_if_conversation_moved
from app.services.importing.runner import COLLECTION_METHODS
from app.services.lead_queries import ORG_TYPE_LABELS
from app.services.privacy import email_hash
from app.services.versions import version_of

LEADS, INSTITUTIONS, CONTACTS = "leads", "institutions", "contacts"
_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
ORG_TYPE_BY_LABEL = {label: code for code, label in ORG_TYPE_LABELS.items()}
AISHE_RE = re.compile(r"^[CSU]-\d{3,7}$")


class EditError(ValueError):
    """A value that can't be saved; the message is shown to the person."""


@dataclass(frozen=True)
class Field:
    column: str  # column label in the table
    entity: str  # 'lead' | 'org' | 'contact'
    attr: str  # model attribute
    kind: str  # text, email, phone, url, bool, stage, user, option:<list>, products, lost_reason, state,
    #            org_type, aishe, area
    required: bool = False


# Columns that can be edited in each table. Anything else shown is read-only.
GRID_FIELDS: dict[str, list[Field]] = {
    LEADS: [
        Field("Institution", "org", "name", "text", required=True),
        Field("Stage", "lead", "stage", "stage", required=True),
        Field("Owner", "lead", "owner", "user"),
        Field("Tier", "org", "tier", "option:tier"),
        Field("Potential", "lead", "potential", "option:potential"),
        Field("Channel", "lead", "channel", "option:lead_channel", required=True),
        Field("NQM", "lead", "nqm_flag", "bool"),
        Field("Product fit", "lead", "product_fit", "products"),
        Field("Interest areas", "lead", "interest_areas", "text"),
        Field("Rejected reason", "lead", "lost_reason", "lost_reason"),
        Field("Notes", "lead", "notes", "text"),
    ],
    INSTITUTIONS: [
        Field("Name", "org", "name", "text", required=True),
        Field("Type", "org", "org_type", "org_type", required=True),
        Field("Category", "org", "category", "text"),
        Field("State", "org", "state", "state"),
        Field("Location", "org", "city", "text"),
        Field("Area", "org", "area", "area"),
        Field("Management", "org", "management", "text"),
        Field("University Name", "org", "affiliated_university", "text"),
        Field("Tier", "org", "tier", "option:tier"),
        Field("Website", "org", "website", "url"),
        Field("AISHE Code", "org", "aishe_code", "aishe"),
    ],
    CONTACTS: [
        Field("Name", "contact", "name", "text", required=True),
        Field("Contact type", "contact", "contact_type", "option:contact_type"),
        Field("Designation", "contact", "designation", "text"),
        Field("Department", "contact", "department", "text"),
        Field("Email", "contact", "email", "email"),
        Field("Phone", "contact", "phone", "phone"),
        Field("LinkedIn", "contact", "linkedin_url", "url"),
        Field("Research interests", "contact", "research_interests", "text"),
        Field("Verified", "contact", "verified", "bool"),
        Field("Do not contact", "contact", "do_not_contact", "bool"),
        Field("Notes", "contact", "notes", "text"),
    ],
}
AREAS = ["Rural", "Urban", "Semi-urban"]
_MAX_LEN = {"name": 255, "designation": 160, "department": 160, "category": 80, "management": 80,
            "affiliated_university": 255, "city": 80, "website": 255, "linkedin_url": 255, "phone": 40,
            "contact_type": 60, "tier": 20, "potential": 20, "channel": 40}


# ---------------------------------------------------------------- planning


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    if isinstance(value, float) and pd.isna(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and value.strip() == ""


def _comparable(value: Any) -> Any:
    if hasattr(value, "tolist") and not isinstance(value, str):
        value = value.tolist()  # numpy scalar → Python scalar; numpy array (list cells) → list
    if _blank(value):
        return None
    if isinstance(value, (list, tuple)):
        return tuple(sorted(str(v) for v in value))
    if isinstance(value, str):
        return value.strip()
    return value


def _display(value: Any) -> str:
    value = _comparable(value)
    if value is None:
        return "—"
    if isinstance(value, tuple):
        return ", ".join(value) or "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


@dataclass
class RowEdit:
    row_id: int
    label: str
    values: dict[str, tuple[Any, Any]]  # column → (old, new)


@dataclass
class EditPlan:
    grid: str
    edits: list[RowEdit] = field(default_factory=list)
    deletes: list[tuple[int, str]] = field(default_factory=list)  # (row id, label)

    @property
    def is_empty(self) -> bool:
        return not self.edits and not self.deletes

    def describe(self) -> pd.DataFrame:
        rows = [{"Row": e.label, "Field": col, "From": _display(old), "To": _display(new)}
                for e in self.edits for col, (old, new) in e.values.items()]
        rows += [{"Row": label, "Field": "(whole row)", "From": "", "To": "DELETE"} for _, label in self.deletes]
        return pd.DataFrame(rows, columns=["Row", "Field", "From", "To"])


def plan_edits(grid: str, original: pd.DataFrame, edited: pd.DataFrame, label_column: str) -> EditPlan:
    """Compares the table as loaded with the table as edited. Both are indexed by the row's id."""
    plan = EditPlan(grid)
    columns = [f.column for f in GRID_FIELDS[grid] if f.column in original.columns]
    for row_id in original.index:
        label = str(original.at[row_id, label_column])
        if row_id not in edited.index:
            plan.deletes.append((int(row_id), label))
            continue
        changed = {}
        for column in columns:
            old, new = original.at[row_id, column], edited.at[row_id, column]
            if _comparable(old) != _comparable(new):
                changed[column] = (old, new)
        if changed:
            plan.edits.append(RowEdit(int(row_id), label, changed))
    return plan


# ---------------------------------------------------------------- applying


@dataclass
class EditResult:
    saved_rows: int = 0
    deleted: int = 0
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Choices:
    """Valid dropdown values, loaded once per save."""

    def __init__(self, session: Session) -> None:
        self.stages = {s.name: s for s in session.scalars(select(Stage))}
        self.users = {u.name: u for u in session.scalars(select(User).where(User.is_active.is_(True)))}
        self.products = {p.name: p for p in session.scalars(select(Product))}
        self.lost_reasons = {r.name: r for r in session.scalars(select(LostReason))}
        self.options: dict[str, set[str]] = {}
        for option in session.scalars(select(OptionList).where(OptionList.is_active.is_(True))):
            self.options.setdefault(option.list_name, set()).add(option.value)
        self.stage_change_type = session.scalar(select(ActivityType).where(ActivityType.name == "Stage change"))


def _text(value: Any) -> str | None:
    return None if _blank(value) else str(value).strip()


def _convert(f: Field, value: Any, choices: _Choices) -> Any:
    """Turns a table cell into the value stored in the database, or raises EditError."""
    if f.kind == "bool":
        return bool(_comparable(value)) if not _blank(value) else False
    if f.kind == "products":
        names = list(_comparable(value) or ())
        unknown = [n for n in names if n not in choices.products]
        if unknown:
            raise EditError(f"unknown product(s): {', '.join(unknown)}")
        return [choices.products[n] for n in names]
    text = _text(value)
    if text is None:
        if f.required:
            raise EditError(f"{f.column} can't be empty")
        return None
    if f.attr in _MAX_LEN and len(text) > _MAX_LEN[f.attr]:
        raise EditError(f"{f.column} is longer than {_MAX_LEN[f.attr]} characters")
    if f.kind == "text":
        return text
    if f.kind == "email":
        if not EMAIL_RE.fullmatch(text):
            raise EditError(f"'{text}' doesn't look like an email address")
        return text.lower()
    if f.kind == "phone":
        phone, _ = parse_phones(text)
        if not phone or sum(ch.isdigit() for ch in phone) < 6:
            raise EditError(f"'{text}' doesn't look like a phone number")
        return phone
    if f.kind == "url":
        return text if "://" in text else f"https://{text}"
    if f.kind == "stage":
        if text not in choices.stages:
            raise EditError(f"unknown stage '{text}'")
        return choices.stages[text]
    if f.kind == "user":
        if text not in choices.users:
            raise EditError(f"'{text}' isn't an active team member")
        return choices.users[text]
    if f.kind == "lost_reason":
        if text not in choices.lost_reasons:
            raise EditError(f"unknown rejected reason '{text}'")
        return choices.lost_reasons[text]
    if f.kind.startswith("option:"):
        list_name = f.kind.split(":", 1)[1]
        if text not in choices.options.get(list_name, set()):
            raise EditError(f"'{text}' isn't one of the {f.column} options")
        return text
    if f.kind == "state":
        state = match_state(text)
        if state is None:
            raise EditError(f"'{text}' isn't an Indian state or union territory")
        return state
    if f.kind == "area":
        if text not in AREAS:
            raise EditError(f"Area must be one of {', '.join(AREAS)}")
        return text
    if f.kind == "org_type":
        if text not in ORG_TYPE_BY_LABEL:
            raise EditError(f"unknown type '{text}'")
        return ORG_TYPE_BY_LABEL[text]
    if f.kind == "aishe":
        code = text.upper().replace(" ", "")
        if not AISHE_RE.match(code):
            raise EditError(f"'{text}' doesn't look like an AISHE code (e.g. C-12345)")
        return code
    raise EditError(f"can't edit {f.column}")


def _audit_value(value: Any) -> Any:
    if isinstance(value, list):
        return [getattr(v, "name", str(v)) for v in value]
    if hasattr(value, "name") and not isinstance(value, str):
        return value.name
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _load_rows(session: Session, grid: str, ids: list[int]) -> dict[int, Any]:
    if not ids:
        return {}
    if grid == LEADS:
        query = select(Lead).where(Lead.id.in_(ids)).options(
            selectinload(Lead.organisation), selectinload(Lead.product_fit), selectinload(Lead.stage),
            selectinload(Lead.owner), selectinload(Lead.lost_reason),
        )
        return {lead.id: lead for lead in session.scalars(query)}
    model = Organisation if grid == INSTITUTIONS else Contact
    return {row.id: row for row in session.scalars(select(model).where(model.id.in_(ids)))}


def current_version(grid: str, row: Any) -> str:
    if grid == LEADS:
        return version_of(row.updated_at, row.organisation.updated_at)
    return version_of(row.updated_at)


def _target(grid: str, row: Any, entity: str) -> Any:
    return row.organisation if (grid == LEADS and entity == "org") else row


def _check_unique(session: Session, attr: str, value: Any, row_id: int | None) -> None:
    if value is None:
        return
    other = -1 if row_id is None else row_id
    if attr == "email":
        clash = session.scalar(select(Contact.id).where(func.lower(Contact.email) == value, Contact.id != other))
        if clash:
            raise EditError(f"another contact already has the email {value}")
    if attr == "aishe_code":
        clash = session.scalar(select(Organisation.name).where(func.upper(Organisation.aishe_code) == value,
                                                             Organisation.id != other))
        if clash:
            raise EditError(f"AISHE code {value} already belongs to {clash}")


def _require(user: User | None, role: str) -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def apply_plan(session: Session, plan: EditPlan, versions: dict[int, str], user: User) -> EditResult:
    """Validates and saves the plan. `versions` holds each row's version_of(...) from when the table loaded."""
    result = EditResult()
    if plan.edits:
        _require(user, "member")
    if plan.deletes:
        _require(user, "admin")
    choices = _Choices(session)
    fields = {f.column: f for f in GRID_FIELDS[plan.grid]}
    rows = _load_rows(session, plan.grid, [e.row_id for e in plan.edits] + [d for d, _ in plan.deletes])
    now = datetime.now(timezone.utc)

    for edit in plan.edits:
        row = rows.get(edit.row_id)
        if row is None:
            result.conflicts.append(f"{edit.label}: no longer exists (deleted by someone else)")
            continue
        if versions.get(edit.row_id) != current_version(plan.grid, row):
            result.conflicts.append(f"{edit.label}: changed by someone else since you opened the table — "
                                    "refresh and redo this row")
            continue
        try:
            ops = []  # (object, attribute, new value) — collected first so a bad cell blocks the whole row
            for column, (_old, new) in edit.values.items():
                f = fields[column]
                value = _convert(f, new, choices)
                target = _target(plan.grid, row, f.entity)
                _check_unique(session, f.attr, value, target.id)
                ops.append((target, f, value))
            if plan.grid == LEADS:
                ops = _lead_stage_rules(row, ops, fields)
        except EditError as error:
            result.errors.append(f"{edit.label}: {error}")
            continue

        diffs: dict[tuple[str, int], dict[str, list[Any]]] = {}
        stage_move = None
        for target, f, value in ops:
            old = getattr(target, f.attr)
            if f.attr == "stage" and old is not value:
                stage_move = (old.name if old else None, value.name)
            diffs.setdefault((target.__tablename__, target.id), {})[f.column] = [_audit_value(old),
                                                                                 _audit_value(value)]
            _set(session, target, f, value, user, now)
        if plan.grid == LEADS and any(f.attr == "product_fit" for _, f, _ in ops):
            row.updated_at = func.now()  # list changes don't touch the lead row; bump it for the conflict check
        for (table, entity_id), diff in diffs.items():
            session.add(AuditLog(user_id=user.id, entity=table, entity_id=str(entity_id), action="update",
                                 diff=diff))
        if stage_move:
            row.stage_changed_at = now
            session.flush()
            stop_if_conversation_moved(session, row, user)
        if stage_move and choices.stage_change_type is not None:
            session.add(Activity(
                activity_type=choices.stage_change_type, lead=row, organisation=row.organisation,
                title=f"Stage: {stage_move[0] or '—'} → {stage_move[1]}", status="done", occurred_at=now,
                direction="internal", owner=user, created_by_user_id=user.id,
            ))
        result.saved_rows += 1

    for row_id, label in plan.deletes:
        row = rows.get(row_id)
        if row is None:
            continue
        try:
            _delete(session, plan.grid, row, user)
            result.deleted += 1
        except EditError as error:
            result.errors.append(f"{label}: {error}")
    session.flush()
    return result


def _lead_stage_rules(lead: Lead, ops: list, fields: dict[str, Field]) -> list:
    """Rejected needs a reason; leaving Rejected clears it."""
    new_stage = next((v for _, f, v in ops if f.attr == "stage"), lead.stage)
    reason_op = next(((t, f, v) for t, f, v in ops if f.attr == "lost_reason"), None)
    reason = reason_op[2] if reason_op else lead.lost_reason
    if new_stage.outcome == "lost" and reason is None:
        raise EditError(f"pick a Rejected reason when setting the stage to {new_stage.name}")
    if new_stage.outcome != "lost" and reason is not None:
        ops = [op for op in ops if op[1].attr != "lost_reason"] + [(lead, fields["Rejected reason"], None)]
    return ops


def _set(session: Session, target: Any, f: Field, value: Any, user: User, now: datetime) -> None:
    if f.attr == "product_fit":
        target.product_fit[:] = value
        return
    setattr(target, f.attr, value)
    if isinstance(target, Organisation) and f.attr == "website":
        target.domain = domain_from_website(value)
    if isinstance(target, Contact) and f.attr == "verified":
        target.verified_by_user_id = user.id if value else None
        target.verified_on = now.date() if value else None
    if isinstance(target, Contact) and f.attr == "do_not_contact":
        _set_do_not_contact(session, target, value, user, now)


def _set_do_not_contact(session: Session, contact: Contact, on: bool, user: User, now: datetime) -> None:
    contact.do_not_contact_on = now.date() if on else None
    contact.do_not_contact_reason = (contact.do_not_contact_reason or f"Marked in the app by {user.name}") if on \
        else None
    if not contact.email:
        return
    digest = email_hash(contact.email)
    existing = session.scalar(select(Suppression).where(Suppression.email_sha256 == digest))
    if on and existing is None:
        session.add(Suppression(email_sha256=digest, reason=contact.do_not_contact_reason, added_by_user_id=user.id))
    elif not on and existing is not None:
        session.delete(existing)


def _delete(session: Session, grid: str, row: Any, user: User) -> None:
    if grid == INSTITUTIONS:
        leads = session.scalar(select(func.count(Lead.id)).where(Lead.org_id == row.id)) or 0
        contacts = session.scalar(select(func.count(Contact.id)).where(Contact.org_id == row.id)) or 0
        if leads or contacts:
            raise EditError(f"still has {leads} lead(s) and {contacts} contact(s) — delete those first")
    snapshot = {c.key: _audit_value(getattr(row, c.key)) for c in row.__table__.columns
                if c.key not in ("created_at", "updated_at")}
    table = row.__tablename__
    session.add(AuditLog(user_id=user.id, entity=table, entity_id=str(row.id), action="delete", diff=snapshot))
    session.delete(row)


# ---------------------------------------------------------------- adding a contact by hand


@dataclass
class NewContact:
    org_id: int
    name: str
    email: str | None = None
    phone: str | None = None
    designation: str | None = None
    contact_type: str | None = None
    department: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None
    collection_method: str = "Other"


def add_contact(session: Session, data: NewContact, user: User) -> Contact:
    """Creates one contact with its consent-log entry and audit record. Raises EditError on bad input."""
    _require(user, "member")
    choices = _Choices(session)
    org = session.get(Organisation, data.org_id)
    if org is None:
        raise EditError("Pick an institution.")
    fields = {f.attr: f for f in GRID_FIELDS[CONTACTS]}
    values = {}
    for attr in ("name", "email", "phone", "designation", "contact_type", "department", "linkedin_url", "notes"):
        values[attr] = _convert(fields[attr], getattr(data, attr), choices)
    if values["email"]:
        _check_unique(session, "email", values["email"], None)
        if session.scalar(select(Suppression.id).where(Suppression.email_sha256 == email_hash(values["email"]))):
            raise EditError("This email is on the do-not-contact list.")
    contact = Contact(org_id=org.id, source=f"Added in the app by {user.name}", **values)
    session.add(contact)
    session.flush()
    session.add(ConsentLog(
        contact=contact, source=f"Added by hand: {data.collection_method}"[:160],
        basis=COLLECTION_METHODS.get(data.collection_method, "other"),
        purpose="BD outreach for Demo products", captured_by_user_id=user.id,
    ))
    session.add(AuditLog(user_id=user.id, entity="contacts", entity_id=str(contact.id), action="create",
                         diff={k: v for k, v in values.items() if v is not None} | {"institution": org.name}))
    return contact


def institution_choices(session: Session, search: str, limit: int = 50) -> list[tuple[int, str]]:
    """Institutions whose name contains the search text, for the Add contact form."""
    text = (search or "").strip()
    if len(text) < 3:
        return []
    rows = session.execute(
        select(Organisation.id, Organisation.name, Organisation.state, Organisation.aishe_code)
        .where(Organisation.name.ilike(f"%{text}%"))
        .order_by(Organisation.name)
        .limit(limit)
    ).all()
    return [(i, " · ".join(x for x in (name, state, code) if x)) for i, name, state, code in rows]


def dropdown_values(session: Session) -> dict[str, list[str]]:
    """Options for the table dropdowns, in their configured order."""
    out: dict[str, list[str]] = {}
    for option in session.scalars(select(OptionList).where(OptionList.is_active.is_(True))
                                  .order_by(OptionList.list_name, OptionList.sort_order)):
        out.setdefault(option.list_name, []).append(option.value)
    out["stage"] = list(session.scalars(select(Stage.name).where(Stage.is_active.is_(True)).order_by(Stage.sort_order)))
    out["user"] = list(session.scalars(select(User.name).where(User.is_active.is_(True)).order_by(User.id)))
    out["product"] = list(session.scalars(select(Product.name).where(Product.is_active.is_(True))
                                          .order_by(Product.sort_order)))
    out["lost_reason"] = list(session.scalars(select(LostReason.name).where(LostReason.is_active.is_(True))
                                              .order_by(LostReason.sort_order)))
    out["state"] = list(INDIAN_STATES)
    out["area"] = list(AREAS)
    out["org_type"] = list(ORG_TYPE_LABELS.values())
    return out


def org_type_hint(aishe_code: str | None) -> str | None:
    """Used by the Colleges table to suggest a type from a newly typed AISHE code."""
    code = org_type_from_aishe(aishe_code)
    return ORG_TYPE_LABELS.get(code) if code else None


def changed_ids(plan: EditPlan) -> Iterable[int]:
    return [e.row_id for e in plan.edits] + [d for d, _ in plan.deletes]


def set_main_contact(session: Session, lead_id: int, contact_id: int, user: User) -> None:
    """Switches who the lead's conversation runs through (cadence tasks go to this person from now on)."""
    _require(user, "member")
    lead = session.get(Lead, lead_id)
    contact = session.get(Contact, contact_id)
    if lead is None or contact is None or contact.org_id != lead.org_id:
        raise EditError("Pick a contact from this institution.")
    old = session.get(Contact, lead.primary_contact_id).name if lead.primary_contact_id else None
    if lead.primary_contact_id == contact.id:
        return
    lead.primary_contact_id = contact.id
    session.add(AuditLog(user_id=user.id, entity="leads", entity_id=str(lead.id), action="update",
                         diff={"Main contact": [old, contact.name]}))
    session.flush()


def bulk_set_flag(session: Session, grid: str, frame: pd.DataFrame, column: str, value: bool, label_column: str,
                  user: User) -> EditResult:
    """'Mark verified' / 'Do not contact' for many rows at once. `frame` = the selected rows, indexed by id,
    with the column and `_version`. Goes through apply_plan, so it is checked and recorded like any edit."""
    plan = EditPlan(grid)
    for row_id, row in frame.iterrows():
        if bool(row[column]) != value:
            plan.edits.append(RowEdit(int(row_id), str(row[label_column]), {column: (row[column], value)}))
    if plan.is_empty:
        return EditResult()
    return apply_plan(session, plan, frame["_version"].to_dict(), user)
