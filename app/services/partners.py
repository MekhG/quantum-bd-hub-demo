"""Partners and deal registration.

A partner (reseller, distribution partner…) registers a deal for an institution. The hub checks for overlap — is the
institution already our lead, or registered by another partner? — and an admin approves or rejects it. An approved
registration links the lead to the partner (channel Reseller) and is locked to them for `deal_registration_days`
(default 60). Won sales count towards upgrading a reseller to distribution partner (`distributor_threshold`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    AuditLog,
    Contact,
    DealRegistration,
    Lead,
    Opportunity,
    Organisation,
    Partner,
    Product,
    Stage,
    Track,
    User,
)
from app.models.partners import PARTNER_STATUSES
from app.services.cadence import today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
ACTIVE_REGISTRATIONS = ("pending", "approved")


class PartnerError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def setting_int(session: Session, key: str, default: int) -> int:
    row = session.get(AppSetting, key)
    try:
        return int(row.value) if row and row.value else default
    except ValueError:
        return default


def _audit(session: Session, user: User, entity: str, entity_id: int, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity=entity, entity_id=str(entity_id), action=action, diff=diff))


# ---------------------------------------------------------------- partners


@dataclass
class PartnerData:
    model: str
    org_id: int | None = None
    new_org_name: str | None = None
    status: str = "active"
    nda_signed_on: date | None = None
    non_compete: bool = False
    nda_doc_id: int | None = None
    terms_doc_id: int | None = None
    price_list_version: str | None = None
    payment_terms: str | None = None
    contact_id: int | None = None
    owner_id: int | None = None
    territory: str | None = None
    notes: str | None = None


def save_partner(session: Session, data: PartnerData, user: User, partner_id: int | None = None) -> Partner:
    _require(user)
    if not data.model:
        raise PartnerError("Pick the partner model.")
    if data.status not in PARTNER_STATUSES:
        raise PartnerError("Pick a valid status.")
    partner = session.get(Partner, partner_id) if partner_id else None
    if partner_id and partner is None:
        raise PartnerError("That partner no longer exists.")
    if partner is None:
        org = session.get(Organisation, data.org_id) if data.org_id else None
        if org is None:
            name = (data.new_org_name or "").strip()
            if not name:
                raise PartnerError("Pick the partner's company or type its name.")
            org = session.scalar(select(Organisation).where(func.lower(Organisation.name) == name.lower()))
            if org is None:
                org = Organisation(name=name[:255], org_type="partner")
                session.add(org)
                session.flush()
        if session.scalar(select(Partner.id).where(Partner.org_id == org.id)):
            raise PartnerError(f"{org.name} is already a partner.")
        partner = Partner(org_id=org.id)
        session.add(partner)
    if data.contact_id:
        contact = session.get(Contact, data.contact_id)
        if contact is None or (contact.org_id is not None and contact.org_id != (partner.org_id or data.org_id)):
            raise PartnerError("The main contact must belong to the partner's company.")
    before = partner.status if partner_id else None
    partner.model, partner.status, partner.nda_signed_on = data.model, data.status, data.nda_signed_on
    partner.non_compete, partner.nda_doc_id, partner.terms_doc_id = data.non_compete, data.nda_doc_id, data.terms_doc_id
    partner.price_list_version = (data.price_list_version or "").strip()[:60] or None
    partner.payment_terms = (data.payment_terms or "").strip() or None
    partner.contact_id, partner.owner_user_id = data.contact_id, data.owner_id or user.id
    partner.territory = (data.territory or "").strip()[:200] or None
    partner.notes = (data.notes or "").strip() or None
    session.flush()
    _audit(session, user, "partners", partner.id, "update" if partner_id else "create",
           {"Partner": [before, f"{partner.model} · {partner.status}"]})
    return partner


def won_sales(session: Session, partner_id: int) -> int:
    return int(session.scalar(select(func.count(Opportunity.id)).join(Lead, Lead.id == Opportunity.lead_id)
                              .where(Lead.partner_id == partner_id, Opportunity.status == "won")) or 0)


def partners_frame(session: Session) -> pd.DataFrame:
    threshold = setting_int(session, "distributor_threshold", 5)
    regs: dict[int, dict[str, int]] = {}
    for pid, status, n in session.execute(select(DealRegistration.partner_id, DealRegistration.status,
                                                 func.count(DealRegistration.id))
                                          .group_by(DealRegistration.partner_id, DealRegistration.status)):
        regs.setdefault(pid, {})[status] = int(n)
    wins = dict(session.execute(select(Lead.partner_id, func.count(Opportunity.id))
                                .join(Opportunity, Opportunity.lead_id == Lead.id)
                                .where(Lead.partner_id.is_not(None), Opportunity.status == "won")
                                .group_by(Lead.partner_id)).all())
    leads = dict(session.execute(select(Lead.partner_id, func.count(Lead.id)).where(Lead.partner_id.is_not(None))
                                 .group_by(Lead.partner_id)).all())
    rows = session.execute(select(Partner, Organisation.name, User.name)
                           .join(Organisation, Organisation.id == Partner.org_id)
                           .outerjoin(User, User.id == Partner.owner_user_id).order_by(Organisation.name)).all()
    records = []
    for p, name, owner in rows:
        won = int(wins.get(p.id, 0))
        records.append({
            "id": p.id, "Partner": name, "Model": p.model, "Status": p.status.title(),
            "NDA signed": p.nda_signed_on, "Non-compete": p.non_compete, "Territory": p.territory or "",
            "Leads": int(leads.get(p.id, 0)), "Pending": regs.get(p.id, {}).get("pending", 0),
            "Approved": regs.get(p.id, {}).get("approved", 0), "Won sales": won,
            "Upgrade": (f"{min(won, threshold)} of {threshold}" if p.model == "Reseller" else ""),
            "Ready to upgrade": p.model == "Reseller" and won >= threshold, "Owner": owner or "",
        })
    return pd.DataFrame(records, columns=["id", "Partner", "Model", "Status", "NDA signed", "Non-compete", "Territory",
                                          "Leads", "Pending", "Approved", "Won sales", "Upgrade", "Ready to upgrade",
                                          "Owner"])


# ---------------------------------------------------------------- deal registration


def check_overlap(session: Session, org_id: int, partner_id: int | None, today: date | None = None) -> list[str]:
    """What already exists for this institution: our leads, and other partners' registrations."""
    today = today or today_ist()
    notes = []
    for track, stage, owner, lead_partner in session.execute(
            select(Track.name, Stage.name, User.name, Lead.partner_id).join(Track, Track.id == Lead.track_id)
            .join(Stage, Stage.id == Lead.stage_id).outerjoin(User, User.id == Lead.owner_user_id)
            .where(Lead.org_id == org_id)):
        who = ""
        if lead_partner and lead_partner != partner_id:
            other = session.get(Partner, lead_partner)
            who = f", worked by partner {other.organisation.name}" if other else ""
        elif lead_partner == partner_id and partner_id:
            who = ", already linked to this partner"
        notes.append(f"Already our lead in {track} — {stage}" + (f" (owner {owner})" if owner else "") + who + ".")
    for reg, partner_name in session.execute(
            select(DealRegistration, Organisation.name).join(Partner, Partner.id == DealRegistration.partner_id)
            .join(Organisation, Organisation.id == Partner.org_id)
            .where(DealRegistration.org_id == org_id, DealRegistration.status.in_(ACTIVE_REGISTRATIONS))):
        if reg.status == "approved" and reg.expires_on and reg.expires_on < today:
            continue
        mine = reg.partner_id == partner_id
        state = (f"approved until {reg.expires_on:%d %b %Y}" if reg.status == "approved" and reg.expires_on
                 else reg.status)
        notes.append(("This partner already registered it" if mine else f"Registered by {partner_name}") +
                     f" on {reg.registered_on:%d %b %Y} ({state}).")
    return notes


def register_deal(session: Session, *, partner_id: int, user: User, org_id: int | None = None,
                  new_org_name: str | None = None, product_ids: list[int] | None = None,
                  expected_value: str | float | None = None, contact_name: str | None = None,
                  notes: str | None = None, today: date | None = None) -> tuple[DealRegistration, list[str]]:
    """Records a partner's deal as pending, with whatever overlap the check found."""
    _require(user)
    today = today or today_ist()
    partner = session.get(Partner, partner_id)
    if partner is None or partner.status not in ("active", "prospect"):
        raise PartnerError("Pick an active partner.")
    org = session.get(Organisation, org_id) if org_id else None
    if org is None:
        name = (new_org_name or "").strip()
        if not name:
            raise PartnerError("Pick the institution or type its name.")
        org = session.scalar(select(Organisation).where(func.lower(Organisation.name) == name.lower()))
        if org is None:
            lower = name.lower()
            org = Organisation(name=name[:255],
                               org_type="university" if "university" in lower else
                               "college" if "college" in lower else "other")
            session.add(org)
            session.flush()
    if org.id == partner.org_id:
        raise PartnerError("A partner can't register a deal for itself.")
    value = None
    if expected_value not in (None, ""):
        try:
            value = Decimal(str(expected_value).replace(",", "").replace("₹", "").strip())
        except InvalidOperation as error:
            raise PartnerError("Expected value must be a number (₹).") from error
    overlap = check_overlap(session, org.id, partner_id, today)
    reg = DealRegistration(partner_id=partner_id, org_id=org.id, product_ids=list(product_ids or []) or None,
                           expected_value_inr=value, contact_name=(contact_name or "").strip()[:160] or None,
                           registered_on=today, status="pending", conflict_note="\n".join(overlap) or None,
                           notes=(notes or "").strip() or None, created_by_user_id=user.id)
    session.add(reg)
    session.flush()
    _audit(session, user, "deal_registrations", reg.id, "create",
           {"Deal registration": [None, f"{partner.organisation.name} → {org.name}"]})
    return reg, overlap


def approve_registration(session: Session, reg_id: int, *, track_id: int, user: User, note: str | None = None,
                         today: date | None = None) -> tuple[Lead, str]:
    """Admin only. Links (or creates) the institution's lead in the track to the partner, locked for N days."""
    _require(user, "admin")
    today = today or today_ist()
    reg = session.get(DealRegistration, reg_id)
    if reg is None or reg.status != "pending":
        raise PartnerError("Only a pending registration can be approved.")
    track = session.get(Track, track_id)
    if track is None:
        raise PartnerError("Pick the track for the lead.")
    rival = session.scalar(select(DealRegistration).where(
        DealRegistration.org_id == reg.org_id, DealRegistration.partner_id != reg.partner_id,
        DealRegistration.status == "approved", DealRegistration.expires_on >= today))
    if rival is not None:
        raise PartnerError(f"{rival.partner.organisation.name} holds an approved registration for this institution "
                           f"until {rival.expires_on:%d %b %Y}. Reject this one, or wait until it expires.")
    lead = session.scalar(select(Lead).where(Lead.org_id == reg.org_id, Lead.track_id == track.id))
    message = ""
    if lead is not None and lead.partner_id and lead.partner_id != reg.partner_id:
        raise PartnerError("That lead is already linked to another partner.")
    if lead is None:
        first = session.scalar(select(Stage).where(Stage.outcome == "open", Stage.is_active.is_(True))
                               .order_by(Stage.sort_order).limit(1))
        lead = Lead(org_id=reg.org_id, track_id=track.id, stage=first, channel="Reseller",
                    source=f"Partner: {reg.partner.organisation.name}"[:120],
                    owner_user_id=reg.partner.owner_user_id or user.id)
        session.add(lead)
        session.flush()
        message = f"Lead created in {track.name}. "
    lead.partner_id, lead.channel = reg.partner_id, "Reseller"
    for product in session.scalars(select(Product).where(Product.id.in_(reg.product_ids or []))):
        if product not in lead.product_fit:
            lead.product_fit.append(product)
    days = setting_int(session, "deal_registration_days", 60)
    reg.status, reg.lead_id, reg.expires_on = "approved", lead.id, today + timedelta(days=days)
    reg.decided_by_user_id, reg.decided_on, reg.decision_note = user.id, today, (note or "").strip() or None
    _audit(session, user, "deal_registrations", reg.id, "update", {"Status": ["pending", "approved"]})
    session.flush()
    return lead, message + f"Approved — locked to {reg.partner.organisation.name} until {reg.expires_on:%d %b %Y}."


def reject_registration(session: Session, reg_id: int, *, user: User, note: str | None = None,
                        today: date | None = None) -> None:
    _require(user, "admin")
    reg = session.get(DealRegistration, reg_id)
    if reg is None or reg.status != "pending":
        raise PartnerError("Only a pending registration can be rejected.")
    if not (note or "").strip():
        raise PartnerError("Say why (the partner will ask).")
    reg.status, reg.decided_by_user_id, reg.decided_on = "rejected", user.id, today or today_ist()
    reg.decision_note = note.strip()
    _audit(session, user, "deal_registrations", reg.id, "update", {"Status": ["pending", "rejected"]})
    session.flush()


def refresh_registrations(session: Session, today: date | None = None) -> int:
    """Daily: approved registrations become won/lost with their lead, or expire after their lock period."""
    today = today or today_ist()
    changed = 0
    for reg in session.scalars(select(DealRegistration).where(DealRegistration.status == "approved")):
        outcome = reg.lead.stage.outcome if reg.lead is not None else None
        if outcome in ("won", "lost"):
            reg.status = outcome
            changed += 1
        elif reg.expires_on and reg.expires_on < today:
            reg.status = "expired"
            changed += 1
    session.flush()
    return changed


def registrations_frame(session: Session, partner_id: int | None = None) -> pd.DataFrame:
    products = dict(session.execute(select(Product.id, Product.name)).all())
    query = (select(DealRegistration, Organisation.name).join(Organisation, Organisation.id == DealRegistration.org_id)
             .order_by(DealRegistration.status != "pending", DealRegistration.registered_on.desc()))
    if partner_id:
        query = query.where(DealRegistration.partner_id == partner_id)
    partner_names = dict(session.execute(select(Partner.id, Organisation.name)
                                         .join(Organisation, Organisation.id == Partner.org_id)).all())
    return pd.DataFrame([{
        "id": r.id, "Partner": partner_names.get(r.partner_id, ""), "Institution": org, "lead_id": r.lead_id,
        "Products": ", ".join(products.get(p, "") for p in (r.product_ids or [])),
        "Expected value (₹)": float(r.expected_value_inr) if r.expected_value_inr is not None else None,
        "Registered": r.registered_on, "Status": r.status.title(), "Locked until": r.expires_on,
        "Overlap found": r.conflict_note or "", "Decision note": r.decision_note or "",
        "Their contact": r.contact_name or "",
    } for r, org in session.execute(query)], columns=[
        "id", "Partner", "Institution", "lead_id", "Products", "Expected value (₹)", "Registered", "Status",
        "Locked until", "Overlap found", "Decision note", "Their contact"])


def pending_count(session: Session) -> int:
    return int(session.scalar(select(func.count(DealRegistration.id))
                              .where(DealRegistration.status == "pending")) or 0)


__all__ = ["PartnerData", "PartnerError", "approve_registration", "check_overlap", "partners_frame",
           "pending_count", "refresh_registrations", "register_deal", "registrations_frame", "reject_registration",
           "save_partner", "setting_int", "won_sales"]


def upgrade_to_distributor(session: Session, partner_id: int, user: User) -> str:
    """Admin only: a reseller with enough won sales becomes a distribution partner."""
    _require(user, "admin")
    partner = session.get(Partner, partner_id)
    if partner is None:
        raise PartnerError("That partner no longer exists.")
    threshold = setting_int(session, "distributor_threshold", 5)
    won = won_sales(session, partner_id)
    if won < threshold:
        raise PartnerError(f"{partner.organisation.name} has {won} won sale(s); {threshold} are needed.")
    before, partner.model = partner.model, "Distribution partner"
    _audit(session, user, "partners", partner.id, "update", {"Model": [before, partner.model]})
    session.flush()
    return f"{partner.organisation.name} is now a distribution partner."


def partner_detail(session: Session, partner_id: int) -> dict | None:
    p = session.get(Partner, partner_id)
    if p is None:
        return None
    return {
        "id": p.id, "org_id": p.org_id, "name": p.organisation.name, "model": p.model, "status": p.status,
        "nda_signed_on": p.nda_signed_on, "non_compete": p.non_compete, "nda_doc_id": p.nda_doc_id,
        "terms_doc_id": p.terms_doc_id, "price_list_version": p.price_list_version or "",
        "payment_terms": p.payment_terms or "", "contact_id": p.contact_id, "owner_id": p.owner_user_id,
        "territory": p.territory or "", "notes": p.notes or "",
        "contact": f"{p.contact.name}" + (f" · {p.contact.email}" if p.contact.email else "") if p.contact else "",
        "contacts": [(c.id, c.name + (f" ({c.designation})" if c.designation else ""))
                     for c in session.scalars(select(Contact).where(Contact.org_id == p.org_id).order_by(Contact.name))],
        "won": won_sales(session, p.id), "threshold": setting_int(session, "distributor_threshold", 5),
    }
