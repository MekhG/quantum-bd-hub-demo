"""Runs an import: the same code produces the preview (dry run, rolled back) and the real import.

Policy for rows that match something already in the database:
- empty fields are filled in; existing values are never overwritten (differences are reported);
- a lead's stage only moves forward;
- nothing is deleted.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    AuditLog,
    ConsentLog,
    Contact,
    ImportBatch,
    ImportBatchItem,
    Lead,
    Opportunity,
    OpportunityProduct,
    Organisation,
    Stage,
    Track,
    User,
)
from app.services.cadence import on_reply, stop_if_conversation_moved
from app.services.importing.columns import ColumnMapping, map_columns
from app.services.importing.lookups import Lookups, load_lookups
from app.services.importing.normalise import (
    clean,
    domain_from_email,
    domain_from_website,
    match_area,
    default_industry,
    guess_org_type,
    match_org_type,
    match_state,
    norm_key,
    org_type_from_aishe,
    parse_date,
    parse_emails,
    parse_inr,
    parse_location,
    parse_phones,
    parse_yes_no,
)

IST = ZoneInfo("Asia/Kolkata")

KIND_LABELS = {
    "tracker": "Lead tracker",
    "responses": "Responses",
    "clients": "Clients",
    "mini": "Mini products",
    "colleges": "Organisation list",
}

# How the contacts in a sheet were collected → DPDP basis recorded in the consent log.
COLLECTION_METHODS: dict[str, str] = {
    "Organisation website / public directory": "publicly_available",
    "They contacted us (enquiry / reply)": "consent",
    "Event or meeting (shared their details)": "consent",
    "Existing customer (gave details for the purchase)": "legitimate_use",
    "Referral / partner": "other",
    "Other": "other",
}

# Leads come with a source; these defaults are offered per track on the import screen.
DEFAULT_LEAD_SOURCE = {
    "cold_colleges": "Fresh",
    "referrals": "Referral",
    "research_labs": "Research institutes",
    "edtech": "EdTech platforms",
    "schools": "Schools",
    "enterprise": "Enterprise prospect list",
    "individuals": "Mini products",
}

# Free-text cells that mean "we haven't verified this contact" rather than a real value.
_NOT_VERIFIED = ("not verified", "unverified")


@dataclass
class ImportOptions:
    kind: str
    track_id: int | None = None
    lead_source: str | None = None
    collection_method: str = "Organisation website / public directory"
    already_verified: bool = False
    create_delivery_tasks: bool = True
    fill_down: bool = False  # institution name only on the first row of each group
    default_owner_id: int | None = None  # owner when the sheet has no 'Owner' column value
    list_name: str | None = None  # raw college lists: tag every institution with this list name
    file_name: str = ""
    sheet_name: str | None = None


@dataclass
class Issue:
    row: Any  # spreadsheet row number, or 'Tab › row' when several tabs are imported together
    level: str  # 'error' (row skipped) or 'warning' (row imported, but check this)
    message: str


@dataclass
class ImportReport:
    kind: str
    rows_total: int = 0
    rows_imported: int = 0
    counts: Counter = field(default_factory=Counter)
    issues: list[Issue] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)  # one per row, for the preview table
    batch_id: int | None = None
    mapping: ColumnMapping | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    def summary(self) -> dict[str, int]:
        return {k: v for k, v in self.counts.items() if v}


class RowSkipped(Exception):
    """Raised inside a row handler to skip the row with an error message."""


class DuplicateRow(Exception):
    """The same AISHE code appeared earlier in this upload; the later row adds nothing."""


class StateHeading(Exception):
    """A row that only names a state (e.g. 'ANDHRA PRADESH') above a group of organisations."""


def _append_unique(existing: str | None, items: list[str], sep: str = "; ") -> str | None:
    """Adds items to a separated text field, skipping ones already there (ignoring case)."""
    current = [x.strip() for x in (existing or "").split(sep.strip()) if x.strip()]
    seen = {x.lower() for x in current}
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            current.append(item)
            seen.add(item.lower())
    return sep.join(current) or None


def _append_line(existing: str | None, line: str) -> str:
    if existing and line in existing:
        return existing
    return f"{existing}\n{line}" if existing else line


class _Importer:
    def __init__(self, session: Session, options: ImportOptions, user: User | None, lookups: Lookups,
                 report: ImportReport) -> None:
        self.s = session
        self.o = options
        self.user = user
        self.lk = lookups
        self.r = report
        self.created: dict[str, list[Any]] = {
            "organisations": [], "contacts": [], "leads": [], "activities": [], "opportunities": []
        }
        if options.kind == "mini":
            self.track = session.scalar(select(Track).where(Track.code == "individuals"))
        else:
            self.track = session.get(Track, options.track_id) if options.track_id else None
        self.default_owner: User | None = session.get(User, options.default_owner_id) if options.default_owner_id else None
        self.today = date.today()
        self.row: Any = 0
        self._warned: set[str] = set()
        self._seen_orgs: set[int] = set()
        self._seen_contacts: set[int] = set()
        # Values outside a dropdown list, reported once at the end as a summary instead of row by row.
        self.unlisted: dict[str, Counter] = defaultdict(Counter)
        self.interest_items: Counter = Counter()
        # Fill-down memory and state headings.
        self._last: dict[str, str | None] = {}
        self._heading_state: str | None = None

    # ---------- reporting ----------

    def warn(self, message: str) -> None:
        # The same message on every row is noise; report it once, on the first row it happened.
        if message in self._warned:
            return
        self._warned.add(message)
        self.r.issues.append(Issue(self.row, "warning", message))

    def count(self, label: str, n: int = 1) -> None:
        self.r.counts[label] += n

    def summarise(self) -> None:
        actions = {
            "Contact type": "filed as blank type; the exact wording is kept as Designation",
            "Department": "kept exactly as written",
            "Status": "ignored (the stage was left as it was)",
            "Potential": "left blank",
            "Tier": "left blank",
            "Channel": "left as Direct",
            "Organisation type": "guessed from the name",
            "Industry": "left blank",
            "Owner": "not a team member in the app, so recorded as the lead's source",
            "Response source": "treated as Other",
        }
        for label, counter in self.unlisted.items():
            total = sum(counter.values())
            examples = ", ".join(f"{value} ({n})" for value, n in counter.most_common(6))
            more = f" and {len(counter) - 6} more" if len(counter) > 6 else ""
            self.r.issues.append(Issue(
                None, "warning",
                f"{label}: {total} rows used wording not in the list — {actions.get(label, 'left blank')}. "
                f"Most common: {examples}{more}.",
            ))
        if self.interest_items:
            examples = ", ".join(f"{v} ({n})" for v, n in self.interest_items.most_common(8))
            self.r.issues.append(Issue(
                None, "warning",
                f"Product-fit items that aren't products were saved as the lead's Interest areas: {examples}.",
            ))

    # ---------- small helpers ----------

    def _fill(self, obj: Any, attr: str, value: Any) -> None:
        if value not in (None, "") and getattr(obj, attr) in (None, ""):
            setattr(obj, attr, value)

    def _option(self, list_name: str, text: str | None, label: str) -> str | None:
        if not text:
            return None
        matcher = self.lk.options.get(list_name)
        value = matcher.match(text) if matcher else None
        if value is None:
            self.unlisted[label][text] += 1
        return value

    def _owner(self, text: str | None) -> tuple[User | None, str | None]:
        """(team member, name to record as source if the person isn't in the team)."""
        if not text:
            return self.default_owner, None
        user = self.lk.match_user(text)
        if user is None:
            self.unlisted["Owner"][text] += 1
            return self.default_owner, text
        return user, None

    # ---------- institutions ----------

    def apply_fill_down(self, v: dict[str, str | None]) -> None:
        """Handles state heading rows always, and copies the organisation down when fill-down is on."""
        name = v.get("institution")
        has_person = bool(v.get("email") or v.get("contact_name") or v.get("phone"))
        if name and not has_person and match_state(name) and not v.get("aishe"):
            self._heading_state = match_state(name)
            self._last = {}
            raise StateHeading
        group_keys = ("institution", "location", "org_type", "state", "district", "website", "aishe", "tier")
        if self.o.fill_down:
            if name:
                self._last = {k: v.get(k) for k in group_keys}
            elif has_person and self._last.get("institution"):
                for key in group_keys:
                    if not v.get(key):
                        v[key] = self._last.get(key)
        if self._heading_state and not v.get("state"):
            v["state"] = self._heading_state

    def resolve_org(self, values: dict[str, str | None], *, create: bool = True) -> tuple[Organisation, str]:
        name = values.get("institution")
        if not name:
            raise RowSkipped("Organisation name is empty.")
        location = values.get("location")
        area = match_area(location)
        city, state_from_location = (None, None) if area else parse_location(location)
        city = values.get("district") or city
        state = match_state(values.get("state")) or state_from_location
        website = values.get("website")
        aishe = (values.get("aishe") or "").strip().upper() or None
        email_domain = domain_from_email(values.get("_email"))
        domains = [d for d in (domain_from_website(website), email_domain) if d]

        match = self.lk.orgs.find(aishe=aishe, domains=domains, name=name, state=state)
        if match.ambiguous:
            raise RowSkipped(
                f"'{name}' matches more than one organisation in the database. Add a State or AISHE code column."
            )

        # AISHE prefix (C-/S-/U-) is authoritative; otherwise use the Type column.
        type_text = values.get("org_type")
        org_type = org_type_from_aishe(aishe) or match_org_type(type_text)
        list_tags = [self.o.list_name] if self.o.list_name else []
        if type_text and not match_org_type(type_text):
            if self.o.kind == "colleges":
                list_tags.append(type_text)  # e.g. 'Vidyalaxmi': which list it came from, not a type
            elif not org_type:
                self.unlisted["Organisation type"][type_text] += 1

        management, category = values.get("management"), values.get("category")
        if management and "universit" in management.lower():
            # In the combined lists this column holds the *affiliating* university's type for colleges.
            if org_type == "university" and not category:
                category = management
            management = None
        tier = self._option("tier", values.get("tier"), "Tier")
        industry = self._option("industry", values.get("industry"), "Industry")

        if match.org is not None:
            org = match.org
            if id(org) not in self._seen_orgs and org not in self.created["organisations"]:
                self.count("Organisations matched")
            self._seen_orgs.add(id(org))
            for attr, value in (
                ("aishe_code", aishe), ("website", website), ("domain", domain_from_website(website) or email_domain),
                ("state", state), ("city", city), ("area", area), ("affiliated_university", values.get("university")),
                ("management", management), ("category", category),
                ("industry", industry or default_industry(org_type or org.org_type)),
            ):
                self._fill(org, attr, value)
            if tier and org.tier and org.tier != tier:
                self.warn(f"'{org.name}': tier in sheet ({tier}) differs from saved tier ({org.tier}); kept {org.tier}.")
            self._fill(org, "tier", tier)
            if list_tags:
                org.source_lists = _append_unique(org.source_lists, list_tags, sep=", ")
            self.lk.orgs.reindex(org)
            return org, f"matched by {match.how}"

        if not create:
            raise RowSkipped(f"Organisation '{name}' not found. Import it via the tracker first, or check the name.")
        if not org_type:
            if self.o.kind == "colleges" and (aishe or industry in (None, "Higher education")):
                org_type = "college"  # an AISHE-style list: rows are higher-education institutions
            else:
                org_type = guess_org_type(name)
            if not type_text:
                self.count("New organisations without a Type (guessed from the name)")
        org = Organisation(
            name=name[:255],
            org_type=org_type,
            industry=industry or default_industry(org_type),
            aishe_code=aishe,
            website=website,
            domain=domain_from_website(website) or email_domain,
            state=state,
            city=city[:80] if city else None,
            area=area,
            tier=tier,
            affiliated_university=values.get("university"),
            management=management[:80] if management else None,
            category=category[:80] if category else None,
            source_lists=_append_unique(None, list_tags, sep=", "),
        )
        self.s.add(org)
        self.lk.orgs.add(org)
        self.created["organisations"].append(org)
        self.count("Organisations created")
        return org, "new"

    def individual_org(self, name: str, state: str | None) -> Organisation:
        org = Organisation(name=name[:255], org_type="individual", state=state)
        self.s.add(org)
        self.created["organisations"].append(org)
        self.count("Individuals created")
        return org

    # ---------- people ----------

    def resolve_contact(self, org: Organisation, values: dict[str, str | None], basis_source: str) -> Contact | None:
        email, extra_emails, bad_email = parse_emails(values.get("email"))
        name = values.get("contact_name")
        if bad_email:
            self.warn(f"Row {self.row}: email '{values.get('email')}' isn't a valid address; kept in the contact's notes.")
        if not email and not name:
            return None
        if self.lk.is_suppressed(email):
            self.count("Contacts skipped (do-not-contact)")
            return None

        phone, extra_phones = parse_phones(values.get("phone"))
        type_text = values.get("contact_type")
        verification_note = None
        if type_text and any(flag in type_text.lower() for flag in _NOT_VERIFIED):
            verification_note, type_text = type_text, None
        contact_type = self._option("contact_type", type_text, "Contact type")
        designation = values.get("designation") or type_text  # keep the exact wording
        department = self._department(values.get("department"))
        if values.get("department") and any(f in values["department"].lower() for f in _NOT_VERIFIED):
            verification_note, department = values["department"], None
        extras = [f"Other emails: {', '.join(extra_emails)}" if extra_emails else "",
                  f"Other numbers: {', '.join(extra_phones)}" if extra_phones else "",
                  f"Unparsed email: {values.get('email')}" if bad_email else "",
                  f"About: {values['about']}" if values.get("about") else ""]
        note = "; ".join(x for x in extras if x) or None

        contact = self.lk.contact_by_email.get(email) if email else None
        if contact is None and name:
            contact = self.lk.contact_by_org_name.get((id(org), norm_key(name)))
        if contact is not None:
            if contact.organisation is not None and contact.organisation is not org:
                self.warn(
                    f"{email or name} is already saved under '{contact.organisation.name}'; "
                    "kept there and linked this row to that contact."
                )
            if contact.do_not_contact:
                self.warn(f"{email or name} is marked do-not-contact.")
            for attr, value in (("phone", phone), ("contact_type", contact_type), ("department", department),
                                ("designation", designation[:160] if designation else None),
                                ("research_interests", values.get("research")), ("email", email),
                                ("linkedin_url", values.get("linkedin")), ("notes", note),
                                ("verification_note", verification_note)):
                self._fill(contact, attr, value)
            if contact.name.startswith("(name unknown") and name:
                contact.name = name[:160]
            if contact.organisation is None and contact.org_id is None:
                contact.organisation = org
            if id(contact) not in self._seen_contacts and contact not in self.created["contacts"]:
                self.count("Contacts matched")
            self._seen_contacts.add(id(contact))
            return contact

        contact = Contact(
            organisation=org,
            name=(name or f"(name unknown – {email})")[:160],
            email=email,
            phone=phone,
            contact_type=contact_type,
            department=department,
            designation=designation[:160] if designation else None,
            research_interests=values.get("research"),
            linkedin_url=(values.get("linkedin") or "")[:255] or None,
            source=basis_source[:120],
            notes=note,
            verification_note=verification_note[:255] if verification_note else None,
        )
        if self.o.already_verified and email and not verification_note:
            contact.verified = True
            contact.verified_on = self.today
            contact.verified_by_user_id = self.user.id if self.user else None
            contact.verification_note = "Marked verified at import"
        self.s.add(contact)
        self.s.add(
            ConsentLog(
                contact=contact,
                source=basis_source[:160],
                basis=COLLECTION_METHODS.get(self.o.collection_method, "other"),
                purpose="BD outreach for Demo products",
                captured_by_user_id=self.user.id if self.user else None,
            )
        )
        if email:
            self.lk.contact_by_email[email] = contact
        if name:
            self.lk.contact_by_org_name[(id(org), norm_key(name))] = contact
        self.created["contacts"].append(contact)
        self.count("Contacts created")
        return contact

    def _department(self, text: str | None) -> str | None:
        if not text:
            return None
        matcher = self.lk.options.get("department")
        value = matcher.match(text) if matcher else None
        if value is None:
            self.unlisted["Department"][text] += 1
            return text[:160]
        return value

    # ---------- leads ----------

    def get_or_create_lead(self, org: Organisation, track_id: int, source: str | None,
                           owner: User | None) -> tuple[Lead, bool]:
        key = (id(org), track_id)
        lead = self.lk.leads.get(key)
        if lead is not None:
            if owner and lead.owner_user_id is None and lead.owner is None:
                lead.owner = owner
            self._fill(lead, "source", source)
            return lead, False
        lead = Lead(
            organisation=org,
            track_id=track_id,
            stage=self.lk.first_stage,
            source=source,
            owner=owner,
        )
        self.s.add(lead)
        self.lk.leads[key] = lead
        self.lk.leads_by_org.setdefault(id(org), []).append(lead)
        self.created["leads"].append(lead)
        self.count("Leads created")
        return lead, True

    def advance_stage(self, lead: Lead, target_name: str | Stage) -> None:
        before = lead.stage
        self._advance_stage(lead, target_name)
        if lead.stage is not before and before is not None:
            lead.stage_changed_at = datetime.now(IST)
            if lead.cadence_status:
                self.s.flush()
                stop_if_conversation_moved(self.s, lead, self.user)

    def _advance_stage(self, lead: Lead, target_name: str | Stage) -> None:
        """Moves a lead forward only. A won lead never changes.

        Ghosted/Rejected from a sheet only replace the first stage (New); a new reply or
        status revives a Ghosted/Rejected lead.
        """
        target = self.lk.stage(target_name) if isinstance(target_name, str) else target_name
        current = lead.stage
        if current is None:
            lead.stage = target
            return
        if current.id == target.id or current.outcome == "won":
            return
        if target.outcome == "won":
            lead.stage = target
            return
        still_early = current.outcome == "open" and current.sort_order <= self.lk.first_stage.sort_order
        if target.outcome in ("lost", "nurture"):
            if still_early:
                lead.stage = target
            return
        if current.outcome in ("lost", "nurture") or target.sort_order > current.sort_order:
            lead.stage = target

    def apply_channel(self, lead: Lead, text: str | None) -> None:
        channel = self._option("lead_channel", text, "Channel")
        # 'Direct' is only the default, so a sheet saying Reseller or Referral may replace it.
        if channel and (lead.channel in (None, "", "Direct")):
            lead.channel = channel

    def apply_product_fit(self, lead: Lead, *texts: str | None) -> None:
        interests: list[str] = []
        for text in texts:
            if not text:
                continue
            products, others = self.lk.match_products(text)
            for product in products:
                if product not in lead.product_fit:
                    lead.product_fit.append(product)
            interests.extend(others)
        if interests:
            for item in interests:
                self.interest_items[item] += 1
            lead.interest_areas = _append_unique(lead.interest_areas, interests)

    def lead_notes(self, lead: Lead, v: dict[str, str | None]) -> None:
        for key, label in (("funding", "Funding"), ("segment", "Segment"), ("why", "Why it qualifies"),
                           ("dept_to_scrape", "Department to scrape"), ("notes", None)):
            if v.get(key):
                lead.notes = _append_line(lead.notes, f"{label}: {v[key]}" if label else v[key])

    def log_inbound(self, *, lead: Lead, org: Organisation, contact: Contact | None, when: date,
                    type_name: str, title: str, notes: str | None, owner: User | None, seen: set[tuple]) -> bool:
        signature = (id(org), when, norm_key(notes or title), id(contact) if contact else None)
        if signature in seen:
            self.count("Entries skipped (already logged)")
            return False
        seen.add(signature)
        activity = Activity(
            activity_type=self.lk.activity_types[type_name],
            lead=lead,
            organisation=org,
            title=title[:200],
            status="done",
            occurred_at=datetime.combine(when, time(12, 0), tzinfo=IST),
            direction="inbound",
            owner=owner,
            notes=notes or None,
            created_by_user_id=self.user.id if self.user else None,
        )
        if contact is not None:
            activity.participants.append(ActivityParticipant(contact=contact))
        self.s.add(activity)
        self.created["activities"].append(activity)
        return True

    # ---------- per-kind handlers ----------

    def tracker_row(self, v: dict[str, str | None], agg: dict[int, dict[str, Any]]) -> str:
        if self.track is None:
            raise RowSkipped("Choose a track for this import.")
        org, how = self.resolve_org(v)
        owner, owner_as_source = self._owner(v.get("owner"))
        lead, is_new = self.get_or_create_lead(org, self.track.id, owner_as_source or self.o.lead_source, owner)
        self.apply_channel(lead, v.get("channel"))
        if parse_yes_no(v.get("nqm")):
            lead.nqm_flag = True
        elif self.lk.funding_means_nqm(v.get("funding")) and not lead.nqm_flag:
            lead.nqm_flag = True
            self.count("Leads marked NQM (from Funding)")
        contact = self.resolve_contact(org, v, f"Import: {self.o.file_name} ({KIND_LABELS['tracker']})")
        if contact is not None and lead.primary_contact is None and lead.primary_contact_id is None:
            lead.primary_contact = contact

        bucket = agg.setdefault(id(lead), {"lead": lead, "statuses": [], "potentials": []})
        status = v.get("status")
        if status:
            stage = self.lk.find_stage(status)
            if stage is None:
                self.unlisted["Status"][status] += 1
            else:
                bucket["statuses"].append(stage)
        potential = self._option("potential", v.get("potential"), "Potential")
        if potential:
            bucket["potentials"].append(potential)
        self.apply_product_fit(lead, v.get("product_fit"), v.get("product_fit2"))
        self.lead_notes(lead, v)
        return f"Organisation {how}; " + ("contact added" if contact else "no contact on this row")

    def finish_stages(self, agg: dict[int, dict[str, Any]]) -> None:
        for bucket in agg.values():
            lead: Lead = bucket["lead"]
            stages = bucket["statuses"]
            if stages:
                won = [s for s in stages if s.outcome == "won"]
                open_ = sorted((s for s in stages if s.outcome == "open"), key=lambda s: s.sort_order)
                quiet = [s for s in stages if s.outcome in ("nurture", "lost")]
                best = won[0] if won else (open_[-1] if open_ else quiet[0])
                before = lead.stage.name if lead.stage else None
                self.advance_stage(lead, best.name)
                if lead.stage.name != before and lead not in self.created["leads"]:
                    self.count("Lead stages moved forward")
            if bucket["potentials"]:
                best_potential = max(bucket["potentials"], key=lambda p: self.lk.option_rank("potential", p))
                if self.lk.option_rank("potential", best_potential) > self.lk.option_rank("potential", lead.potential):
                    lead.potential = best_potential

    def responses_row(self, v: dict[str, str | None], seen: set[tuple]) -> str:
        if self.track is None:
            raise RowSkipped("Choose a track for this import.")
        when = parse_date(v.get("date"))
        if when is None:
            raise RowSkipped(f"Date '{v.get('date') or ''}' is missing or not a date.")
        source_text = v.get("source")
        source = self.lk.options["response_source"].match(source_text) if source_text else None
        if source is None:
            self.unlisted["Response source"][source_text or "(blank)"] += 1
            source = "Other"
        is_reply = source == "Email outreach"
        if self.lk.is_suppressed(v.get("_email")):
            raise RowSkipped("Contact is on the do-not-contact list; response not logged.")

        org, how = self.resolve_org(v, create=not is_reply)
        contact = self.resolve_contact(
            org, v, "Replied to our email outreach" if is_reply else f"Inbound: {source}",
        )
        owner, _ = self._owner(v.get("owner"))
        lead = self._lead_for_response(org, source, owner)
        self.apply_channel(lead, v.get("channel"))
        if is_reply:
            # A reply stops any running cadence and moves the lead to Replied to Queries (decided 2026-10-19).
            self.s.flush()
            before = lead.stage
            on_reply(self.s, lead, self.user)
            if lead.stage is not before and lead not in self.created["leads"]:
                self.count("Lead stages moved forward")
        if contact is not None and lead.primary_contact is None and lead.primary_contact_id is None:
            lead.primary_contact = contact

        text = v.get("response") or ""
        if not text:
            self.warn(f"Row {self.row}: response text is empty.")
        type_name = {"Email outreach": "Email", "Website enquiry": "Website enquiry"}.get(source, "Other enquiry")
        who = contact.name if contact else org.name
        title = f"Reply to email outreach — {who}" if is_reply else f"{source} — {who}"
        if not self.log_inbound(lead=lead, org=org, contact=contact, when=when, type_name=type_name, title=title,
                                notes=text, owner=owner, seen=seen):
            return "Already logged earlier — skipped"
        self.count("Responses logged")
        return f"Organisation {how}; response logged on {when:%d %b %Y}"

    def _lead_for_response(self, org: Organisation, source: str, owner: User | None) -> Lead:
        leads = self.lk.leads_by_org.get(id(org), [])
        in_track = [l for l in leads if l.track_id == self.track.id]
        if in_track:
            return in_track[0]
        if len(leads) == 1:
            return leads[0]
        if len(leads) > 1:
            self.warn(f"'{org.name}' is in several tracks; response added to its {leads[0].track.name} lead.")
            return leads[0]
        lead_source = "Email outreach" if source == "Email outreach" else f"Inbound – {source}"
        if source == "Email outreach":
            self.warn(f"'{org.name}' had no lead yet; created one in {self.track.name}.")
        lead, _ = self.get_or_create_lead(org, self.track.id, lead_source, owner)
        return lead

    def clients_row(self, v: dict[str, str | None], deals: dict[tuple, Opportunity]) -> str:
        if self.track is None:
            raise RowSkipped("Choose a track for this import.")
        po_date = parse_date(v.get("date"))
        if v.get("date") and po_date is None:
            raise RowSkipped(f"Date '{v.get('date')}' isn't a date.")
        products, unknown = self.lk.match_products(v.get("products"))
        if unknown:
            self.warn(f"Row {self.row}: products not recognised: {', '.join(unknown)}.")

        org, how = self.resolve_org(v)
        contact = self.resolve_contact(org, v, f"Client record: {self.o.file_name}")
        owner, owner_as_source = self._owner(v.get("owner"))
        lead, _ = self.get_or_create_lead(org, self.track.id, owner_as_source or self.o.lead_source or "Client", owner)
        self.apply_channel(lead, v.get("channel"))
        self.advance_stage(lead, self.lk.won_stage)
        if contact is not None and lead.primary_contact is None and lead.primary_contact_id is None:
            lead.primary_contact = contact

        if po_date is None and not products:
            self.count("Clients recorded without PO details")
            return f"Organisation {how}; recorded as a client (no PO date or products yet)"

        key = (id(lead), po_date)
        deal = deals.get(key)
        if deal is None:
            deal = next((o for o in lead.opportunities if o.po_date == po_date), None) if lead.id else None
        if deal is None:
            label = f"PO {po_date:%d %b %Y}" if po_date else "PO date not recorded"
            deal = Opportunity(lead=lead, title=f"{org.name} — {label}"[:200], status="won",
                               po_date=po_date, expected_close=po_date)
            self.s.add(deal)
            self.created["opportunities"].append(deal)
            self.count("Deals created")
        deals[key] = deal
        value = parse_inr(v.get("value"))
        if value and deal.value_inr is None:
            deal.value_inr = Decimal(str(round(value, 2)))
        if po_date is None:
            self.count("Deals without PO date (no delivery dates)")

        existing_products = {item.product.id for item in deal.line_items}
        added = []
        for product in products:
            if product.id in existing_products:
                continue
            due = po_date + timedelta(days=product.delivery_days or 0) if po_date else None
            deal.line_items.append(OpportunityProduct(product=product, expected_delivery_on=due))
            existing_products.add(product.id)
            added.append(product.name)
            if due is None:
                continue
            if self.o.create_delivery_tasks and due >= self.today:
                task = Activity(
                    activity_type=self.lk.activity_types["Follow-up task"],
                    lead=lead,
                    organisation=org,
                    title=f"Delivery due: {product.name} — {org.name}"[:200],
                    status="planned",
                    scheduled_at=datetime.combine(due, time(10, 0), tzinfo=IST),
                    direction="internal",
                    owner=lead.owner or owner or self.user,
                    created_by_user_id=self.user.id if self.user else None,
                )
                self.s.add(task)
                self.created["activities"].append(task)
                self.count("Delivery follow-ups scheduled")
            elif due < self.today:
                self.count("Deliveries already past due date (not scheduled)")
        return f"Organisation {how}; " + (f"products: {', '.join(added)}" if added else "no new products")

    def mini_row(self, v: dict[str, str | None], agg: dict[int, dict[str, Any]], seen: set[tuple]) -> str:
        if self.track is None:
            raise RowSkipped("The Mini products track is missing. Run: python -m app.seed.seed_config")
        name, email = v.get("contact_name"), v.get("_email")
        if not name and not email:
            raise RowSkipped("Name and email are both empty.")
        if self.lk.is_suppressed(email):
            raise RowSkipped("This person is on the do-not-contact list.")
        when = parse_date(v.get("date"))
        if v.get("date") and when is None:
            self.warn(f"Row {self.row}: date '{v['date']}' isn't a date; no dated entry logged.")

        existing = self.lk.contact_by_email.get(email) if email else None
        if v.get("institution"):
            org, how = self.resolve_org(v)
        elif existing is not None and existing.organisation is not None:
            org, how = existing.organisation, "matched by email"
        else:
            org, how = self.individual_org(name or email, match_state(v.get("state"))), "new individual"

        owner, owner_as_source = self._owner(v.get("owner"))
        lead, _ = self.get_or_create_lead(org, self.track.id, owner_as_source or self.o.lead_source, owner)
        self.apply_channel(lead, v.get("channel"))
        contact = self.resolve_contact(org, v, f"Mini products: {self.o.file_name}")
        if contact is not None and lead.primary_contact is None and lead.primary_contact_id is None:
            lead.primary_contact = contact

        bucket = agg.setdefault(id(lead), {"lead": lead, "statuses": [], "potentials": []})
        if v.get("status"):
            stage = self.lk.find_stage(v["status"])
            if stage is None:
                self.unlisted["Status"][v["status"]] += 1
            else:
                bucket["statuses"].append(stage)
        potential = self._option("potential", v.get("potential"), "Potential")
        if potential:
            bucket["potentials"].append(potential)
        self.apply_product_fit(lead, v.get("product_fit"))
        if v.get("notes"):
            lead.notes = _append_line(lead.notes, v["notes"])

        if when is not None:
            interest = v.get("product_fit") or "Mini products"
            self.log_inbound(lead=lead, org=org, contact=contact, when=when, type_name="Other enquiry",
                             title=f"Showed interest in {interest}", notes=v.get("notes"), owner=owner, seen=seen)
        return f"{how}; " + (f"interest logged on {when:%d %b %Y}" if when else "no date given")

    def colleges_row(self, v: dict[str, str | None], seen_aishe: set[str]) -> str:
        if not v.get("institution"):
            raise RowSkipped("Name is empty.")
        aishe = (v.get("aishe") or "").strip().upper() or None
        if aishe and aishe in seen_aishe:
            raise DuplicateRow
        if aishe:
            seen_aishe.add(aishe)
        _, how = self.resolve_org(v)
        return "New organisation" if how == "new" else f"Organisation {how}"


def _row_values(row: pd.Series, mapping: ColumnMapping) -> dict[str, str | None]:
    values = {key: clean(row.get(header)) for key, header in mapping.columns.items()}
    values["_email"] = parse_emails(values.get("email"))[0]
    return values


def _row_label(row_no: Any) -> Any:
    return int(row_no) if isinstance(row_no, (int,)) or str(row_no).isdigit() else str(row_no)


def run_import(session: Session, frame: pd.DataFrame, options: ImportOptions, user: User | None,
               *, dry_run: bool) -> ImportReport:
    """Preview (dry_run=True: nothing is saved) or import (dry_run=False: caller commits)."""
    report = ImportReport(kind=options.kind, rows_total=len(frame))
    mapping = map_columns(list(frame.columns), options.kind)
    report.mapping = mapping
    if not mapping.ok:
        missing = ", ".join(f.label for f in mapping.missing_required)
        report.issues.append(Issue(None, "error", f"Required column(s) missing: {missing}."))
        return report

    lookups = load_lookups(session, include_people=options.kind != "colleges")
    if options.kind != "colleges" and "response_source" not in lookups.options:
        report.issues.append(Issue(None, "error", "Option lists are missing. Run: python -m app.seed.seed_config"))
        return report
    importer = _Importer(session, options, user, lookups, report)

    stage_agg: dict[int, dict[str, Any]] = {}
    seen: set[tuple] = (
        _existing_inbound_signatures(session, lookups) if options.kind in ("responses", "mini") else set()
    )
    deals: dict[tuple, Opportunity] = {}
    seen_aishe: set[str] = set()

    with session.no_autoflush:
        for row_no, row in frame.iterrows():
            importer.row = _row_label(row_no)
            values = _row_values(row, mapping)
            issues_before = len(report.issues)
            try:
                if options.kind in ("tracker", "clients", "responses"):
                    importer.apply_fill_down(values)
                if options.kind == "tracker":
                    outcome = importer.tracker_row(values, stage_agg)
                elif options.kind == "responses":
                    outcome = importer.responses_row(values, seen)
                elif options.kind == "clients":
                    outcome = importer.clients_row(values, deals)
                elif options.kind == "mini":
                    outcome = importer.mini_row(values, stage_agg, seen)
                else:
                    outcome = importer.colleges_row(values, seen_aishe)
                report.rows_imported += 1
                status = "warning" if len(report.issues) > issues_before else "ok"
            except DuplicateRow:
                report.counts["Repeated rows in this upload (skipped)"] += 1
                outcome, status = "Same AISHE code as an earlier row", "repeat"
            except StateHeading:
                report.counts["State heading rows (used, not imported)"] += 1
                outcome, status = "State heading", "heading"
            except RowSkipped as skipped:
                report.issues.append(Issue(importer.row, "error", str(skipped)))
                outcome, status = str(skipped), "skipped"
            report.outcomes.append({
                "Row": str(importer.row),
                "Organisation": values.get("institution") or "",
                "Contact": values.get("contact_name") or values.get("_email") or "",
                "Result": status,
                "Details": outcome,
            })
        if options.kind in ("tracker", "mini"):
            importer.finish_stages(stage_agg)
    importer.summarise()

    if dry_run and options.kind == "colleges":
        # The raw lists can hold tens of thousands of rows; the preview stops before writing anything.
        session.rollback()
        return report

    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        report.issues.append(Issue(None, "error", f"The database rejected the import: {error.orig}"))
        report.rows_imported = 0
        return report

    if dry_run:
        session.rollback()
        return report

    batch = ImportBatch(
        kind=options.kind,
        file_name=options.file_name[:255],
        sheet_name=(options.sheet_name or "")[:120] or None,
        track_id=importer.track.id if importer.track else None,
        imported_by_user_id=user.id if user else None,
        rows_total=report.rows_total,
        # A list keeps the order people read it in (Postgres re-sorts JSON object keys).
        summary=[[label, value] for label, value in report.summary().items()],
    )
    session.add(batch)
    for entity, objects in importer.created.items():
        for obj in objects:
            batch.items.append(ImportBatchItem(entity=entity, entity_id=obj.id))
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            entity="import_batches",
            action="import",
            diff={"kind": options.kind, "file": options.file_name, **report.summary()},
        )
    )
    session.flush()
    report.batch_id = batch.id
    return report


def _existing_inbound_signatures(session: Session, lookups: Lookups) -> set[tuple]:
    """Inbound entries already in the database, so re-importing the same sheet doesn't duplicate them."""
    signatures: set[tuple] = set()
    contact_by_id = {c.id: c for c in lookups.contact_by_email.values()}
    contact_by_id.update({c.id: c for c in lookups.contact_by_org_name.values()})
    rows = session.execute(
        select(Activity.org_id, Activity.occurred_at, Activity.notes, Activity.title, ActivityParticipant.contact_id)
        .outerjoin(ActivityParticipant, ActivityParticipant.activity_id == Activity.id)
        .where(Activity.direction == "inbound")
    )
    for org_id, occurred_at, notes, title, contact_id in rows:
        org = lookups.org_by_id.get(org_id) if org_id else None
        if org is None or occurred_at is None:
            continue
        contact = contact_by_id.get(contact_id) if contact_id else None
        when = occurred_at.astimezone(IST).date() if occurred_at.tzinfo else occurred_at.date()
        signatures.add((id(org), when, norm_key(notes or title), id(contact) if contact else None))
    return signatures
