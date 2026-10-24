"""In-memory indexes of what's already in the database, so each row is matched without a query.

Loading everything once and matching in Python keeps imports fast over the
internet connection to Supabase, even for tens of thousands of rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ActivityType,
    Contact,
    Lead,
    Opportunity,
    OptionList,
    Organisation,
    Product,
    Stage,
    Suppression,
    User,
)
from app.services.importing.normalise import (
    OptionMatcher,
    domain_candidates,
    names_look_alike,
    norm_key,
    split_list,
)
from app.services.privacy import email_hash


_ALL_PRODUCTS = {"all 5", "all five", "all products", "all 5 products", "all five products"}

# When a contact type names two roles ('HOD / Professor'), file it under the more senior one.
CONTACT_TYPE_PRIORITY = ["Director/Principal", "Dean", "Registrar", "HoD", "Purchase/Admin", "Faculty", "Other"]


@dataclass
class OrgMatch:
    org: Any | None
    how: str = ""  # 'AISHE code', 'website domain', 'name + state', 'name'
    ambiguous: bool = False


class OrgIndex:
    """Finds an institution by AISHE code → web/email domain → name + state → name (only if unique)."""

    def __init__(self) -> None:
        self.by_aishe: dict[str, Any] = {}
        self.by_domain: dict[str, Any] = {}
        self.by_name_state: dict[tuple[str, str], list[Any]] = defaultdict(list)
        self.by_name: dict[str, list[Any]] = defaultdict(list)

    def add(self, org: Any) -> None:
        # Individuals (Mini-product buyers without an institution) are found via their email, never by name:
        # two different people can share a name.
        if getattr(org, "org_type", None) == "individual":
            return
        if org.aishe_code:
            self.by_aishe[org.aishe_code.upper()] = org
        if org.domain:
            self.by_domain.setdefault(org.domain, org)
        name = norm_key(org.name)
        self.by_name[name].append(org)
        if org.state:
            self.by_name_state[(name, norm_key(org.state))].append(org)

    def reindex(self, org: Any) -> None:
        """Call after filling in an AISHE code or domain on an existing organisation."""
        if org.aishe_code:
            self.by_aishe.setdefault(org.aishe_code.upper(), org)
        if org.domain:
            self.by_domain.setdefault(org.domain, org)

    def find(self, *, aishe: str | None, domains: list[str], name: str, state: str | None) -> OrgMatch:
        code = aishe.upper() if aishe else None
        if code and code in self.by_aishe:
            return OrgMatch(self.by_aishe[code], "AISHE code")

        def compatible(org: Any) -> bool:
            # Two different AISHE codes are two different institutions, whatever their names.
            return not (code and org.aishe_code and org.aishe_code.upper() != code)

        for domain in domains:
            for candidate in domain_candidates(domain):
                hit = self.by_domain.get(candidate)
                # Constituent colleges often share a university's domain, so the names must agree too.
                if hit is not None and compatible(hit) and names_look_alike(hit.name, name):
                    return OrgMatch(hit, "website/email domain")
        key = norm_key(name)
        if state:
            hits = [o for o in self.by_name_state.get((key, norm_key(state)), []) if compatible(o)]
            if len(hits) == 1:
                return OrgMatch(hits[0], "name + state")
            if len(hits) > 1:
                return OrgMatch(None, ambiguous=not code)
        hits = [o for o in self.by_name.get(key, []) if compatible(o)]
        if len(hits) == 1:
            # A same-named institution in a different state is a different institution.
            if state and hits[0].state and norm_key(hits[0].state) != norm_key(state):
                return OrgMatch(None)
            return OrgMatch(hits[0], "name")
        if len(hits) > 1:
            return OrgMatch(None, ambiguous=not code)
        return OrgMatch(None)


@dataclass
class Lookups:
    orgs: OrgIndex
    org_by_id: dict[int, Organisation]
    contact_by_email: dict[str, Contact]
    contact_by_org_name: dict[tuple[int, str], Contact]  # (id(org object), name key)
    leads: dict[tuple[int, int], Lead]  # (id(org object), track_id)
    leads_by_org: dict[int, list[Lead]]  # id(org object) → leads in any track
    suppressed: set[str]
    stages: dict[str, Stage]  # lower-case name → stage
    activity_types: dict[str, ActivityType]
    users_by_key: dict[str, User]
    products: list[Product]
    options: dict[str, OptionMatcher]
    option_order: dict[str, list[str]]
    # Normalised phrases from the 'nqm_funding' list; kept as data so the team can add wording later.
    nqm_funding_keys: list[str] = field(default_factory=list)
    product_matcher: OptionMatcher = field(init=False)

    def __post_init__(self) -> None:
        self.product_matcher = OptionMatcher([(p.name, p.aliases) for p in self.products])
        self._product_by_name = {p.name: p for p in self.products}

    # ---- helpers used by the import handlers ----

    def stage(self, name: str) -> Stage:
        return self.stages[name.lower()]

    def find_stage(self, text: str | None) -> Stage | None:
        """Sheet wording → stage, using the stage's name or any of its aliases."""
        return self.stages.get(norm_key(text)) if text else None

    @property
    def first_stage(self) -> Stage:
        """Where new leads start (the first open stage, 'New' today)."""
        return min((s for s in self.stages.values() if s.outcome == "open" and s.is_active),
                   key=lambda s: s.sort_order)

    @property
    def won_stage(self) -> Stage:
        return min((s for s in self.stages.values() if s.outcome == "won" and s.is_active),
                   key=lambda s: s.sort_order)

    def match_user(self, text: str | None) -> User | None:
        """'Riya' or 'Riya Sharma' → that user; first recognised name wins for 'Kabir/Neha'."""
        if not text:
            return None
        for piece in split_list(text) or [text]:
            key = norm_key(piece)
            if key in self.users_by_key:
                return self.users_by_key[key]
            first = key.split(" ")[0] if key else ""
            if first in self.users_by_key:
                return self.users_by_key[first]
        return None

    def match_products(self, text: str | None) -> tuple[list[Product], list[str]]:
        """Returns (recognised products, other items such as 'Quantum Awareness' kept as interest areas)."""
        found: list[Product] = []
        unknown: list[str] = []
        if text and norm_key(text) in _ALL_PRODUCTS:
            return [p for p in self.products if not p.is_mini], []
        pieces = split_list(text, slash=False)
        whole = self.product_matcher.match(text) if text else None
        if whole and len(pieces) <= 1:
            return [self._product_by_name[whole]], []
        for piece in pieces:
            # 'QKD / NetSim' is two products, but 'Faculty Training/Enablement' is one interest.
            parts = [p.strip() for p in piece.split("/") if p.strip()]
            names = [self.product_matcher.match(p) for p in parts]
            if len(parts) > 1 and all(names):
                matched = names
            else:
                single = self.product_matcher.match(piece)
                matched = [single] if single else []
            if not matched:
                unknown.append(piece)
            for name in matched:
                product = self._product_by_name[name]
                if product not in found:
                    found.append(product)
        return found, unknown

    def is_suppressed(self, email: str | None) -> bool:
        return bool(email) and email_hash(email) in self.suppressed

    def funding_means_nqm(self, text: str | None) -> bool:
        """'UG Lab', 'UG Lab (applied)', 'NQM - UG lab' → True. Whole-word match so 'drug lab' doesn't count."""
        if not text:
            return False
        padded = f" {norm_key(text)} "
        return any(f" {key} " in padded for key in self.nqm_funding_keys)

    def option_rank(self, list_name: str, value: str | None) -> int:
        order = self.option_order.get(list_name, [])
        return order.index(value) if value in order else -1


def _stage_index(session: Session) -> dict[str, Stage]:
    index: dict[str, Stage] = {}
    for stage in session.scalars(select(Stage).order_by(Stage.sort_order)):
        index[stage.name.lower()] = stage
        index.setdefault(norm_key(stage.name), stage)
    for stage in session.scalars(select(Stage).order_by(Stage.sort_order)):
        for alias in stage.aliases or []:
            index.setdefault(norm_key(alias), stage)  # a real stage name always wins over an alias
    return index


def load_lookups(session: Session, *, include_people: bool = True) -> Lookups:
    """include_people=False skips contacts/leads (the raw college import doesn't need them)."""
    orgs = OrgIndex()
    org_by_id: dict[int, Organisation] = {}
    for org in session.scalars(select(Organisation)):
        orgs.add(org)
        org_by_id[org.id] = org

    contact_by_email: dict[str, Contact] = {}
    contact_by_org_name: dict[tuple[int, str], Contact] = {}
    leads: dict[tuple[int, int], Lead] = {}
    leads_by_org: dict[int, list[Lead]] = defaultdict(list)
    if include_people:
        for contact in session.scalars(select(Contact)):
            if contact.email:
                contact_by_email[contact.email.lower()] = contact
            org = org_by_id.get(contact.org_id) if contact.org_id else None
            if org is not None:
                contact_by_org_name[(id(org), norm_key(contact.name))] = contact
        # Load product fit and deals up front: one query each instead of one per lead.
        lead_query = select(Lead).options(
            selectinload(Lead.product_fit),
            selectinload(Lead.opportunities).selectinload(Opportunity.line_items),
        )
        for lead in session.scalars(lead_query):
            org = org_by_id[lead.org_id]
            leads[(id(org), lead.track_id)] = lead
            leads_by_org[id(org)].append(lead)

    users_by_key: dict[str, User] = {}
    for user in session.scalars(select(User).where(User.is_active.is_(True))):
        full = norm_key(user.name)
        users_by_key.setdefault(full, user)
        users_by_key.setdefault(full.split(" ")[0], user)

    options: dict[str, OptionMatcher] = {}
    option_order: dict[str, list[str]] = {}
    grouped: dict[str, list[tuple[str, list[str] | None]]] = defaultdict(list)
    for option in session.scalars(
        select(OptionList).where(OptionList.is_active.is_(True)).order_by(OptionList.sort_order)
    ):
        grouped[option.list_name].append((option.value, option.aliases))
    for list_name, entries in grouped.items():
        options[list_name] = OptionMatcher(
            entries,
            compact=(list_name == "potential"),
            priority=CONTACT_TYPE_PRIORITY if list_name == "contact_type" else None,
        )
        option_order[list_name] = [value for value, _ in entries]

    return Lookups(
        orgs=orgs,
        org_by_id=org_by_id,
        contact_by_email=contact_by_email,
        contact_by_org_name=contact_by_org_name,
        leads=leads,
        leads_by_org=leads_by_org,
        suppressed=set(session.scalars(select(Suppression.email_sha256))),
        stages=_stage_index(session),
        activity_types={t.name: t for t in session.scalars(select(ActivityType))},
        users_by_key=users_by_key,
        products=list(session.scalars(select(Product).where(Product.is_active.is_(True)))),
        options=options,
        option_order=option_order,
        nqm_funding_keys=sorted(
            {norm_key(text) for value, aliases in grouped.get("nqm_funding", [])
             for text in [value, *(aliases or [])] if norm_key(text)}
        ),
    )
