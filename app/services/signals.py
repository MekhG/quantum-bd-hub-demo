"""Signals and the lead score.

A lead's score = the points of its institution's active signals (each signal type counted once — the most recent —
so five news items about the same grant don't stack) + automatic points the hub works out from its own data:

- Open quotation: a quote on one of the lead's deals is sent and still valid.
- Demo held: the latest demo done on the lead, fading over the type's months.
- Replied recently: the latest reply on the lead, fading over the type's months (default 3, i.e. ~90 days).
- NQM applicant: the lead's NQM flag.

Points fade in a straight line: a 12-month signal is worth full points on day one, half at six months, nothing
after twelve. Won and lost leads score 0 — there is nothing left to prioritise.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityType,
    AppSetting,
    AuditLog,
    Lead,
    Opportunity,
    Organisation,
    OrgSignal,
    Quote,
    SignalType,
    Stage,
    Track,
    User,
)
from app.services.cadence import ist_date, today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
DAYS_PER_MONTH = 30.44
DEFAULT_HOT_SCORE = 40


class SignalError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def hot_score(session: Session) -> int:
    """Leads at or above this score are 'hot' (Configuration → News & signals → Settings)."""
    value = session.get(AppSetting, "hot_lead_score")
    try:
        return int(value.value) if value and value.value else DEFAULT_HOT_SCORE
    except ValueError:
        return DEFAULT_HOT_SCORE


def points_now(weight: int, decay_months: int, observed_on: date, today: date) -> int:
    """Full points on the day, fading in a straight line to zero over decay_months."""
    if weight <= 0:
        return 0
    window = max(decay_months, 0) * DAYS_PER_MONTH
    age = (today - observed_on).days
    if age < 0:
        age = 0
    if window <= 0 or age >= window:
        return 0
    return round(weight * (1 - age / window))


def expires_on(decay_months: int, observed_on: date) -> date:
    return observed_on + timedelta(days=round(decay_months * DAYS_PER_MONTH))


# ---------------------------------------------------------------- signal types


def manual_types(session: Session) -> list[SignalType]:
    """Types people can add by hand (automatic ones are computed)."""
    return list(session.scalars(select(SignalType).where(SignalType.is_active.is_(True), SignalType.code.is_(None))
                                .order_by(SignalType.sort_order, SignalType.name)))


def types_frame(session: Session) -> pd.DataFrame:
    rows = session.scalars(select(SignalType).order_by(SignalType.code.is_not(None), SignalType.sort_order,
                                                       SignalType.name))
    return pd.DataFrame([{
        "id": t.id, "Signal": t.name, "Points": t.weight, "Fades over (months)": t.decay_months,
        "Headline words": ", ".join(t.keywords or []), "Automatic": t.code is not None,
        "What it means": t.description or "", "Active": t.is_active,
    } for t in rows], columns=["id", "Signal", "Points", "Fades over (months)", "Headline words", "Automatic",
                               "What it means", "Active"])


def save_types(session: Session, frame: pd.DataFrame, user: User) -> int:
    """Saves the Configuration editor. New rows become manual types; automatic types keep their name and code."""
    _require(user, "admin")
    existing = {t.id: t for t in session.scalars(select(SignalType))}
    names = [str(n).strip() for n in frame["Signal"].fillna("") if str(n).strip()]
    if len(names) != len({n.lower() for n in names}):
        raise SignalError("Two signal types have the same name.")
    changed = 0
    for row in frame.to_dict("records"):
        name = str(row.get("Signal") or "").strip()
        if not name:
            continue
        try:
            weight = int(row.get("Points") or 0)
            months = int(row.get("Fades over (months)") or 12)
        except (TypeError, ValueError) as error:
            raise SignalError(f"{name}: points and months must be whole numbers.") from error
        if not 0 <= weight <= 100 or not 1 <= months <= 60:
            raise SignalError(f"{name}: points must be 0–100 and months 1–60.")
        words = [w.strip().lower() for w in str(row.get("Headline words") or "").split(",") if w.strip()]
        values = {"weight": weight, "decay_months": months, "keywords": words or None,
                  "description": str(row.get("What it means") or "").strip() or None,
                  "is_active": bool(row.get("Active", True))}
        row_id = row.get("id")
        kind = existing.get(int(row_id)) if row_id is not None and row_id == row_id else None
        if kind is None:
            session.add(SignalType(name=name[:80], sort_order=len(existing) + changed + 1, **values))
            changed += 1
            continue
        if kind.code is None:
            values["name"] = name[:80]
        if any(getattr(kind, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(kind, k, v)
            changed += 1
    if changed:
        session.add(AuditLog(user_id=user.id, entity="signal_types", entity_id="*", action="update",
                             diff={"Signal types": [None, f"{changed} change(s)"]}))
        session.flush()
    return changed


# ---------------------------------------------------------------- adding and removing signals


def add_signal(session: Session, *, org_id: int, type_id: int, observed_on: date, user: User,
               source_url: str | None = None, note: str | None = None,
               news_item_id: int | None = None) -> tuple[OrgSignal, bool]:
    """Saves a signal against an institution. Returns (signal, created) — the same signal twice is not duplicated."""
    _require(user)
    org = session.get(Organisation, org_id)
    kind = session.get(SignalType, type_id)
    if org is None:
        raise SignalError("Pick an institution.")
    if kind is None or not kind.is_active:
        raise SignalError("Pick a signal type.")
    if kind.code is not None:
        raise SignalError(f"“{kind.name}” is worked out by the hub automatically — it can't be added by hand.")
    if observed_on > today_ist():
        raise SignalError("The date can't be in the future.")
    url = (source_url or "").strip() or None
    if url and not re.match(r"^https?://\S+$", url):
        raise SignalError("The source link must start with https://")
    same = session.scalar(select(OrgSignal).where(OrgSignal.org_id == org_id, OrgSignal.signal_type_id == type_id,
                                                  OrgSignal.observed_on == observed_on))
    if same is not None and (same.source_url or None) == url:
        return same, False
    signal = OrgSignal(org_id=org_id, signal_type_id=type_id, observed_on=observed_on, source_url=url,
                       note=(note or "").strip() or None, news_item_id=news_item_id, created_by_user_id=user.id)
    session.add(signal)
    session.flush()
    session.add(AuditLog(user_id=user.id, entity="organisations", entity_id=str(org_id), action="update",
                         diff={"Signal added": [None, f"{kind.name} ({observed_on:%d %b %Y})"]}))
    return signal, True


def remove_signal(session: Session, signal_id: int, user: User) -> str:
    """The person who added a signal, or an admin, can remove it."""
    _require(user)
    signal = session.get(OrgSignal, signal_id)
    if signal is None:
        raise SignalError("That signal was already removed.")
    if user.role != "admin" and signal.created_by_user_id != user.id:
        raise PermissionError("Only the person who added this signal, or an admin, can remove it.")
    label = f"{signal.signal_type.name} ({signal.observed_on:%d %b %Y})"
    org_id = signal.org_id
    session.delete(signal)
    session.add(AuditLog(user_id=user.id, entity="organisations", entity_id=str(org_id), action="update",
                         diff={"Signal removed": [label, None]}))
    session.flush()
    recompute_scores(session, org_ids=[org_id])
    return label


# ---------------------------------------------------------------- scoring


@dataclass(frozen=True)
class Part:
    """One line of a lead's score."""

    signal: str
    points: int
    when: date | None
    source: str | None
    signal_id: int | None   # set for signals people saved (removable)
    auto: bool


def _org_parts(session: Session, org_ids: list[int] | None, today: date) -> dict[int, list[Part]]:
    query = (select(OrgSignal, SignalType).join(SignalType, SignalType.id == OrgSignal.signal_type_id)
             .where(SignalType.is_active.is_(True)).order_by(OrgSignal.observed_on.desc(), OrgSignal.id.desc()))
    if org_ids is not None:
        query = query.where(OrgSignal.org_id.in_(org_ids))
    out: dict[int, list[Part]] = {}
    seen: set[tuple[int, int]] = set()
    for signal, kind in session.execute(query):
        points = points_now(kind.weight, kind.decay_months, signal.observed_on, today)
        counted = (signal.org_id, kind.id) not in seen and points > 0
        if counted:
            seen.add((signal.org_id, kind.id))
        out.setdefault(signal.org_id, []).append(
            Part(kind.name, points if counted else 0, signal.observed_on, signal.source_url, signal.id, False))
    return out


def _auto_types(session: Session) -> dict[str, SignalType]:
    return {t.code: t for t in session.scalars(select(SignalType).where(SignalType.code.is_not(None),
                                                                        SignalType.is_active.is_(True)))}


def _auto_parts(session: Session, lead_ids: list[int] | None, today: date) -> dict[int, list[Part]]:
    kinds = _auto_types(session)
    out: dict[int, list[Part]] = {}

    def scoped(query, column):
        return query.where(column.in_(lead_ids)) if lead_ids is not None else query

    if (kind := kinds.get("open_quote")) and kind.weight:
        rows = session.execute(scoped(
            select(Opportunity.lead_id, func.max(Quote.sent_on))
            .join(Quote, Quote.opportunity_id == Opportunity.id)
            .where(Quote.status == "sent", Quote.valid_until >= today).group_by(Opportunity.lead_id),
            Opportunity.lead_id))
        for lead_id, sent in rows:
            out.setdefault(lead_id, []).append(Part(kind.name, kind.weight, sent, None, None, True))
    if (kind := kinds.get("demo_held")) and kind.weight:
        rows = session.execute(scoped(
            select(Activity.lead_id, func.max(Activity.occurred_at))
            .join(ActivityType, ActivityType.id == Activity.type_id)
            .where(ActivityType.channel == "demo", Activity.status == "done", Activity.lead_id.is_not(None))
            .group_by(Activity.lead_id), Activity.lead_id))
        for lead_id, when in rows:
            held = ist_date(when) if when else None
            points = points_now(kind.weight, kind.decay_months, held, today) if held else 0
            if points:
                out.setdefault(lead_id, []).append(Part(kind.name, points, held, None, None, True))
    if (kind := kinds.get("replied_recently")) and kind.weight:
        rows = session.execute(scoped(
            select(Activity.lead_id, func.max(Activity.occurred_at))
            .where(Activity.direction == "inbound", Activity.status == "done", Activity.lead_id.is_not(None))
            .group_by(Activity.lead_id), Activity.lead_id))
        for lead_id, when in rows:
            replied = ist_date(when) if when else None
            points = points_now(kind.weight, kind.decay_months, replied, today) if replied else 0
            if points:
                out.setdefault(lead_id, []).append(Part(kind.name, points, replied, None, None, True))
    if (kind := kinds.get("nqm_applicant")) and kind.weight:
        for lead_id in session.scalars(scoped(select(Lead.id).where(Lead.nqm_flag.is_(True)), Lead.id)):
            out.setdefault(lead_id, []).append(Part(kind.name, kind.weight, None, None, None, True))
    return out


def score_parts(session: Session, lead_id: int, today: date | None = None) -> list[Part]:
    """Everything that makes up one lead's score, highest first (signals that no longer count are listed at 0)."""
    today = today or today_ist()
    lead = session.get(Lead, lead_id)
    if lead is None:
        return []
    parts = _org_parts(session, [lead.org_id], today).get(lead.org_id, []) + \
        _auto_parts(session, [lead_id], today).get(lead_id, [])
    return sorted(parts, key=lambda p: (-p.points, p.auto, -(p.when or date.min).toordinal()))


def recompute_scores(session: Session, *, lead_ids: list[int] | None = None, org_ids: list[int] | None = None,
                     today: date | None = None) -> int:
    """Writes leads.score. Returns how many scores changed. Run daily (fading) and after signals change."""
    today = today or today_ist()
    query = select(Lead.id, Lead.org_id, Lead.score, Stage.outcome).join(Stage, Stage.id == Lead.stage_id)
    if lead_ids is not None:
        query = query.where(Lead.id.in_(lead_ids))
    if org_ids is not None:
        query = query.where(Lead.org_id.in_(org_ids))
    leads = session.execute(query).all()
    if not leads:
        return 0
    scope_leads = [lid for lid, *_ in leads] if (lead_ids is not None or org_ids is not None) else None
    scope_orgs = sorted({oid for _, oid, *_ in leads}) if scope_leads is not None else None
    org_parts = _org_parts(session, scope_orgs, today)
    auto = _auto_parts(session, scope_leads, today)
    changed = 0
    for lead_id, org_id, old, outcome in leads:
        new = 0 if outcome in ("won", "lost") else \
            sum(p.points for p in org_parts.get(org_id, [])) + sum(p.points for p in auto.get(lead_id, []))
        if new != old:
            session.get(Lead, lead_id).score = new
            changed += 1
    session.flush()
    return changed


# ---------------------------------------------------------------- lists


def signals_frame(session: Session, today: date | None = None, *, include_faded: bool = False) -> pd.DataFrame:
    """Every saved signal with its points today (the Signals tab and its Excel export)."""
    today = today or today_ist()
    rows = session.execute(
        select(OrgSignal, SignalType, Organisation.name, User.name)
        .join(SignalType, SignalType.id == OrgSignal.signal_type_id)
        .join(Organisation, Organisation.id == OrgSignal.org_id)
        .outerjoin(User, User.id == OrgSignal.created_by_user_id)
        .order_by(OrgSignal.observed_on.desc(), OrgSignal.id.desc())).all()
    records = []
    for signal, kind, org_name, who in rows:
        points = points_now(kind.weight, kind.decay_months, signal.observed_on, today) if kind.is_active else 0
        if not include_faded and points == 0:
            continue
        records.append({"id": signal.id, "org_id": signal.org_id, "Institution": org_name, "Signal": kind.name,
                        "Points now": points, "Seen on": signal.observed_on,
                        "Counts until": expires_on(kind.decay_months, signal.observed_on),
                        "Source": signal.source_url or "", "Note": signal.note or "", "Added by": who or "",
                        "created_by": signal.created_by_user_id})
    return pd.DataFrame(records, columns=["id", "org_id", "Institution", "Signal", "Points now", "Seen on",
                                          "Counts until", "Source", "Note", "Added by", "created_by"])


def hot_leads(session: Session, *, threshold: int | None = None, limit: int = 10) -> pd.DataFrame:
    """Open leads at or above the hot score, highest first."""
    threshold = hot_score(session) if threshold is None else threshold
    rows = session.execute(
        select(Lead.id, Organisation.name, Track.name, Stage.name, Lead.score, User.name)
        .join(Organisation, Organisation.id == Lead.org_id).join(Track, Track.id == Lead.track_id)
        .join(Stage, Stage.id == Lead.stage_id).outerjoin(User, User.id == Lead.owner_user_id)
        .where(Lead.score >= threshold, Stage.outcome.in_(("open", "nurture")))
        .order_by(Lead.score.desc(), Organisation.name).limit(limit)).all()
    return pd.DataFrame(rows, columns=["lead_id", "Institution", "Track", "Stage", "Score", "Owner"])


def lead_orgs(session: Session) -> list[tuple[int, str, list[str]]]:
    """Institutions that have at least one lead: (id, name, aliases). The pool news is matched against."""
    rows = session.execute(select(Organisation.id, Organisation.name, Organisation.aliases)
                           .where(select(Lead.id).where(Lead.org_id == Organisation.id).exists())
                           .order_by(Organisation.name)).all()
    return [(oid, name, list(aliases or [])) for oid, name, aliases in rows]


# ---------------------------------------------------------------- pasting a list of institutions


def normalise(text: str | None) -> str:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[4:] if text.startswith("the ") else text


_SHORT_FORMS = (
    ("indian institute of science education and research ", "iiser "),
    ("indian institute of information technology ", "iiit "),
    ("indian institute of technology ", "iit "),
    ("national institute of technology ", "nit "),
    ("indian institute of management ", "iim "),
)


def name_variants(name: str, aliases: list[str] | None = None) -> set[str]:
    """Normalised ways an institution is written: its name without the city after a comma or text in brackets,
    the bracketed short form ('IISc'), common short forms ('IIT Bombay') and its aliases."""
    variants: set[str] = set()
    base = name or ""
    for bracket in re.findall(r"\(([^)]*)\)", base):
        if bracket.strip():
            variants.add(normalise(bracket))
    base = re.sub(r"\([^)]*\)", " ", base)
    for candidate in (base, base.split(",")[0]):
        norm = normalise(candidate)
        if norm:
            variants.add(norm)
            for long, short in _SHORT_FORMS:
                if norm.startswith(long):
                    variants.add(short + norm[len(long):])
    variants |= {normalise(a) for a in (aliases or []) if normalise(a)}
    return variants


@dataclass
class PasteMatch:
    pasted: str
    org_id: int | None
    org_name: str
    how: str   # 'Exact', 'Close', 'Not found'


def match_names(session: Session, names: list[str]) -> list[PasteMatch]:
    """Each name → the lead institution it most likely is: exact (name, short form or alias) or close spelling."""
    orgs = lead_orgs(session)
    exact: dict[str, tuple[int, str]] = {}
    for oid, name, aliases in orgs:
        for variant in name_variants(name, aliases):
            exact.setdefault(variant, (oid, name))
    keys = list(exact)
    out: list[PasteMatch] = []
    for raw in names:
        pasted = re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", str(raw or "")).strip()
        if not pasted:
            continue
        found = None
        for variant in name_variants(pasted):
            if variant in exact:
                found = (*exact[variant], "Exact")
                break
        if found is None:
            close = difflib.get_close_matches(normalise(pasted), keys, n=1, cutoff=0.82)
            if close:
                found = (*exact[close[0]], "Close")
        out.append(PasteMatch(pasted, found[0], found[1], found[2]) if found else
                   PasteMatch(pasted, None, "", "Not found"))
    return out


def match_pasted_names(session: Session, text: str) -> list[PasteMatch]:
    """One institution per line (e.g. the list in a PIB release)."""
    return match_names(session, text.splitlines())


SHEET_COLUMNS = ["Institution", "Signal", "Seen on", "Source link", "Note"]
MAX_SHEET_BYTES = 2_000_000


def signal_sheet_template() -> bytes:
    import io

    frame = pd.DataFrame([{"Institution": "Example University", "Signal": "NQM lab approved",
                           "Seen on": date.today(), "Source link": "https://pib.gov.in/…", "Note": "Second NQM call"}],
                         columns=SHEET_COLUMNS)
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False, sheet_name="Signals")
    return buffer.getvalue()


def read_signal_sheet(data: bytes, filename: str) -> pd.DataFrame:
    """Reads an uploaded .xlsx/.csv of signals. Only Institution is required; the other columns may be blank."""
    import io

    if len(data) > MAX_SHEET_BYTES:
        raise SignalError("That file is larger than 2 MB — split it or paste the names instead.")
    name = filename.lower()
    try:
        if name.endswith(".csv"):
            frame = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
        elif name.endswith(".xlsx"):
            frame = pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
        else:
            raise SignalError("Upload an .xlsx or .csv file.")
    except SignalError:
        raise
    except Exception as error:  # noqa: BLE001 — any parsing problem is reported plainly
        raise SignalError(f"Couldn't read that file ({type(error).__name__}).") from error
    frame.columns = [str(c).strip() for c in frame.columns]
    lower = {c.lower(): c for c in frame.columns}
    if "institution" not in lower:
        raise SignalError("The sheet needs an 'Institution' column (download the template to see the layout).")
    out = pd.DataFrame({col: frame[lower[col.lower()]] if col.lower() in lower else "" for col in SHEET_COLUMNS})
    out = out[out["Institution"].astype(str).str.strip() != ""].head(500)
    return out.reset_index(drop=True)


def save_signal_rows(session: Session, rows: list[dict], user: User) -> tuple[int, int, list[str]]:
    """Rows of {org_id, type_id, observed_on, source_url, note}. Returns (added, already there, problems)."""
    _require(user)
    added = already = 0
    problems: list[str] = []
    orgs: set[int] = set()
    for row in rows:
        try:
            _, created = add_signal(session, org_id=int(row["org_id"]), type_id=int(row["type_id"]),
                                    observed_on=row["observed_on"], user=user, source_url=row.get("source_url"),
                                    note=row.get("note"))
        except SignalError as error:
            problems.append(f"{row.get('label') or row['org_id']}: {error}")
            continue
        orgs.add(int(row["org_id"]))
        added += created
        already += not created
    if orgs:
        recompute_scores(session, org_ids=sorted(orgs))
    return added, already, problems


def bulk_add(session: Session, org_ids: list[int], *, type_id: int, observed_on: date, user: User,
             source_url: str | None = None, note: str | None = None) -> tuple[int, int]:
    """Adds the same signal to several institutions. Returns (added, already there)."""
    added = already = 0
    for org_id in dict.fromkeys(org_ids):
        _, created = add_signal(session, org_id=org_id, type_id=type_id, observed_on=observed_on, user=user,
                                source_url=source_url, note=note)
        added += created
        already += not created
    recompute_scores(session, org_ids=list(org_ids))
    return added, already
