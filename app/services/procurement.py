"""Procurement & quotes: settings, price list, bundles, deals, procurement checklists and the quote lifecycle.

Quote lifecycle: draft → sent → accepted (PO) | expired | superseded (a newer version was sent) | cancelled.
Sending logs the activity, moves the lead to Sent Quotation and plans an expiry reminder; accepting records the PO,
marks the deal won, moves the lead to the won stage and plans delivery follow-ups (each product's delivery time).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    AppSetting,
    AuditLog,
    Bundle,
    BundleItem,
    Contact,
    Lead,
    Opportunity,
    OpportunityProduct,
    OptionList,
    PriceItem,
    ProcurementItem,
    ProcurementRule,
    Product,
    Quote,
    QuoteLine,
    Stage,
    User,
)
from app.services.cadence import IST, advance_to, at_ist, complete_activity, today_ist
from app.services.money import inr, paise, to_decimal

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
OPEN_QUOTES = ("draft", "sent")
CHECKLIST_LABELS = {"to_do": "To do", "requested": "Requested", "received": "Received", "submitted": "Submitted",
                    "not_needed": "Not needed"}
POTENTIAL_WEIGHT = {"0%": 0.0, "30-50%": 0.4, "50-70%": 0.6, "70-90%": 0.8, "100%": 1.0}


class ProcurementError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _audit(session: Session, user: User, entity: str, entity_id: int | str, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity=entity, entity_id=str(entity_id), action=action, diff=diff))


# ---------------------------------------------------------------- settings


def get_settings(session: Session) -> dict[str, str]:
    return {k: v or "" for k, v in session.execute(select(AppSetting.key, AppSetting.value))}


def save_settings(session: Session, values: dict[str, str], user: User) -> int:
    _require(user, "admin")
    current = {s.key: s for s in session.scalars(select(AppSetting))}
    changed = 0
    for key, value in values.items():
        value = (value or "").strip()
        row = current.get(key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
            changed += 1
        elif (row.value or "") != value:
            _audit(session, user, "app_settings", key, "update", {key: [row.value, value]})
            row.value = value
            changed += 1
    session.flush()
    return changed


def _int_setting(settings: dict[str, str], key: str, default: int) -> int:
    try:
        return int(settings.get(key) or default)
    except ValueError:
        return default


def options(session: Session, list_name: str) -> list[str]:
    return list(session.scalars(select(OptionList.value).where(OptionList.list_name == list_name,
                                                                OptionList.is_active.is_(True))
                                .order_by(OptionList.sort_order)))


# ---------------------------------------------------------------- price list

PRICE_COLUMNS = ["Name", "Details", "Product", "Unit", "Institute price (INR)", "Partner price (INR)", "GST %", "HSN/SAC",
                 "Valid from", "Valid to", "Active"]


def price_items_frame(session: Session, *, include_inactive: bool = True) -> pd.DataFrame:
    query = (select(PriceItem, Product.name).outerjoin(Product, Product.id == PriceItem.product_id)
             .order_by(PriceItem.sort_order, PriceItem.name))
    if not include_inactive:
        query = query.where(PriceItem.is_active.is_(True))
    rows = [{"id": p.id, "Name": p.name, "Details": p.description or "", "Product": product or "", "Unit": p.unit,
             "Institute price (INR)": float(p.institute_price_inr) if p.institute_price_inr is not None else None,
             "Partner price (INR)": float(p.partner_price_inr) if p.partner_price_inr is not None else None,
             "GST %": float(p.gst_pct), "HSN/SAC": p.hsn_sac or "", "Valid from": p.valid_from,
             "Valid to": p.valid_to, "Active": p.is_active}
            for p, product in session.execute(query)]
    return pd.DataFrame(rows, columns=["id", *PRICE_COLUMNS])


def _num(value, label: str, row: int, *, low: float = 0, high: float | None = None) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except Exception as error:  # noqa: BLE001 — any parse failure is the same message
        raise ProcurementError(f"Row {row}: {label} must be a number.") from error
    if number < low or (high is not None and number > high):
        raise ProcurementError(f"Row {row}: {label} must be between {low} and {high}." if high is not None
                               else f"Row {row}: {label} can't be negative.")
    return number


def _date_or_none(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "" or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    stamp = pd.to_datetime(value, dayfirst=True, errors="coerce")
    return None if pd.isna(stamp) else stamp.date()


def save_price_items(session: Session, frame: pd.DataFrame, user: User) -> tuple[int, int]:
    """Upserts the price list from the editable grid or an imported sheet (rows matched by id, else by name).
    Rows are never deleted — untick Active instead, so old quotes and bundles keep their references."""
    _require(user, "admin")
    products = {p.name.lower(): p for p in session.scalars(select(Product))}
    existing = {p.id: p for p in session.scalars(select(PriceItem))}
    by_name = {p.name.lower(): p for p in existing.values()}
    added = updated = 0
    for i, rec in enumerate(frame.to_dict("records"), start=2):
        name = str(rec.get("Name") or "").strip()
        if not name:
            continue
        product_name = str(rec.get("Product") or "").strip()
        product = products.get(product_name.lower()) if product_name else None
        if product_name and product is None:
            raise ProcurementError(f"Row {i}: unknown product '{product_name}'. Use a name from the Products list.")
        gst = _num(rec.get("GST %"), "GST %", i, high=100)
        details = rec.get("Details")
        details = None if details is None or (isinstance(details, float) and pd.isna(details)) else str(details).strip()
        values = dict(
            name=name[:200], description=details or None, product_id=product.id if product else None,
            unit=(str(rec.get("Unit") or "").strip() or "unit")[:40],
            institute_price_inr=_num(rec.get("Institute price (INR)"), "Institute price", i),
            partner_price_inr=_num(rec.get("Partner price (INR)"), "Partner price", i),
            gst_pct=gst if gst is not None else Decimal("18"),
            hsn_sac=(str(rec.get("HSN/SAC") or "").strip() or None),
            valid_from=_date_or_none(rec.get("Valid from")), valid_to=_date_or_none(rec.get("Valid to")),
            is_active=bool(rec.get("Active")) if rec.get("Active") is not None and not (
                isinstance(rec.get("Active"), float) and pd.isna(rec.get("Active"))) else True,
        )
        rid = rec.get("id")
        row = existing.get(int(rid)) if rid is not None and not pd.isna(rid) else by_name.get(name.lower())
        if row is None:
            row = PriceItem(sort_order=len(existing) + added + 1, **values)
            session.add(row)
            added += 1
        else:
            diff = {k: [str(getattr(row, k)), str(v)] for k, v in values.items() if getattr(row, k) != v}
            if diff:
                for k, v in values.items():
                    setattr(row, k, v)
                _audit(session, user, "price_items", row.id, "update", diff)
                updated += 1
    session.flush()
    return added, updated


def price_template() -> pd.DataFrame:
    """An empty sheet with the right headers (Configuration → Price list → Download template)."""
    return pd.DataFrame([{"Name": "QSim Cluster — Quantum Computing Simulator for Academia",
                          "Details": "The paragraph shown under the item name on the quotation.",
                          "Product": "QSim Cluster", "Unit": "licence",
                          "Institute price (INR)": None, "Partner price (INR)": None, "GST %": 18, "HSN/SAC": "",
                          "Valid from": None, "Valid to": None, "Active": True}], columns=PRICE_COLUMNS)


def active_price_items(session: Session, on: date | None = None) -> list[PriceItem]:
    on = on or today_ist()
    return [p for p in session.scalars(select(PriceItem).where(PriceItem.is_active.is_(True))
                                       .order_by(PriceItem.sort_order, PriceItem.name))
            if (p.valid_from is None or p.valid_from <= on) and (p.valid_to is None or p.valid_to >= on)]


# ---------------------------------------------------------------- bundles


def bundles(session: Session) -> list[Bundle]:
    return list(session.scalars(select(Bundle).where(Bundle.is_active.is_(True))
                                .options(selectinload(Bundle.items).selectinload(BundleItem.price_item))
                                .order_by(Bundle.sort_order, Bundle.name)))


def save_bundle(session: Session, *, name: str, phase: str | None, description: str | None,
                items: list[tuple[int, int]], user: User, bundle_id: int | None = None) -> Bundle:
    _require(user, "admin")
    name = (name or "").strip()
    if not name or not items:
        raise ProcurementError("A bundle needs a name and at least one item.")
    bundle = session.get(Bundle, bundle_id) if bundle_id else None
    clash = session.scalar(select(Bundle.id).where(func.lower(Bundle.name) == name.lower()))
    if clash and clash != bundle_id:
        raise ProcurementError("A bundle with that name already exists.")
    if bundle is None:
        bundle = Bundle(sort_order=(session.scalar(select(func.count(Bundle.id))) or 0) + 1)
        session.add(bundle)
    bundle.name, bundle.phase, bundle.description = name[:200], (phase or "").strip() or None, description or None
    bundle.items = [BundleItem(price_item_id=int(pid), quantity=max(int(qty), 1)) for pid, qty in items]
    session.flush()
    return bundle


def delete_bundle(session: Session, bundle_id: int, user: User) -> None:
    _require(user, "admin")
    bundle = session.get(Bundle, bundle_id)
    if bundle is not None:
        bundle.is_active = False
        session.flush()


# ---------------------------------------------------------------- deals and procurement checklists


@dataclass
class DealData:
    title: str
    products: list[tuple[int, int]] = field(default_factory=list)  # (product_id, quantity)
    funding_source: str | None = None
    buying_route: str | None = None
    expected_close: date | None = None
    notes: str | None = None


def suggest_route(management: str | None) -> str | None:
    """Demo is not on GeM, so government institutions buy through the
    proprietary-article (PAC) route, starting with a GeM non-availability certificate. Private institutions issue a
    direct purchase order."""
    text = (management or "").lower()
    if "government" in text or "central" in text or "state" in text or "local body" in text:
        return "PAC (proprietary article)"
    if "private" in text:
        return "Direct purchase order"
    return None


def in_budget_window(day: date | None) -> bool:
    # Indian FY ends 31 March: January–March closes risk fund lapse, so they get pushed first.
    return day is not None and day.month in (1, 2, 3)


def sync_checklist(session: Session, deal: Opportunity) -> tuple[int, int]:
    """Adds the documents the buying route needs; drops untouched items that no longer apply.
    Items someone already worked on (status other than to-do) are always kept."""
    lead = session.get(Lead, deal.lead_id)
    reseller = (lead.channel or "").lower() == "reseller" if lead else False
    wanted = [r.item for r in session.scalars(
        select(ProcurementRule).where(ProcurementRule.is_active.is_(True),
                                      ProcurementRule.buying_route.in_([deal.buying_route or "", "Any"]))
        .order_by(ProcurementRule.sort_order)) if reseller or not r.only_if_reseller] if deal.buying_route else []
    current = list(session.scalars(select(ProcurementItem).where(ProcurementItem.opportunity_id == deal.id)))
    have = {i.item for i in current}
    added = removed = 0
    for order, item in enumerate(wanted, start=1):
        if item not in have:
            session.add(ProcurementItem(opportunity_id=deal.id, item=item, sort_order=order))
            added += 1
    for item in current:
        if item.item not in wanted and item.status == "to_do" and not item.notes and item.document_id is None:
            session.delete(item)
            removed += 1
    session.flush()
    return added, removed


def _apply_deal(session: Session, deal: Opportunity, data: DealData) -> None:
    title = (data.title or "").strip()
    if not title:
        raise ProcurementError("Give the deal a title, e.g. 'Phase 1 — simulation lab'.")
    deal.title = title[:200]
    deal.funding_source, deal.buying_route = data.funding_source or None, data.buying_route or None
    deal.expected_close, deal.notes = data.expected_close, (data.notes or "").strip() or None
    deal.budget_window_flag = in_budget_window(data.expected_close)
    wanted = {int(pid): max(int(q), 1) for pid, q in data.products}
    for item in list(deal.line_items):
        if item.product_id not in wanted:
            deal.line_items.remove(item)
        else:
            item.quantity = wanted.pop(item.product_id)
    for pid, qty in wanted.items():
        deal.line_items.append(OpportunityProduct(product_id=pid, quantity=qty))


def create_deal(session: Session, lead_id: int, data: DealData, user: User) -> Opportunity:
    _require(user)
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ProcurementError("That lead no longer exists.")
    deal = Opportunity(lead_id=lead.id, title="", status="open")
    session.add(deal)
    _apply_deal(session, deal, data)
    session.flush()
    sync_checklist(session, deal)
    _audit(session, user, "opportunities", deal.id, "create", {"Deal": [None, deal.title]})
    session.flush()
    return deal


def update_deal(session: Session, deal_id: int, data: DealData, user: User) -> Opportunity:
    _require(user)
    deal = session.get(Opportunity, deal_id)
    if deal is None:
        raise ProcurementError("That deal no longer exists.")
    before = (deal.title, deal.buying_route, deal.funding_source, deal.expected_close)
    _apply_deal(session, deal, data)
    session.flush()
    sync_checklist(session, deal)
    after = (deal.title, deal.buying_route, deal.funding_source, deal.expected_close)
    labels = ("Title", "Buying route", "Funding", "Expected close")
    _audit(session, user, "opportunities", deal.id, "update",
           {k: [str(a), str(b)] for k, a, b in zip(labels, before, after) if a != b})
    session.flush()
    return deal


def set_checklist_item(session: Session, item_id: int, user: User, *, status: str | None = None,
                       notes: str | None = None, document_id: int | None = None, due_on: date | None = None) -> None:
    _require(user)
    item = session.get(ProcurementItem, item_id)
    if item is None:
        raise ProcurementError("That checklist item no longer exists.")
    if status is not None:
        item.status = status
    if notes is not None:
        item.notes = notes.strip() or None
    if document_id is not None:
        item.document_id = document_id or None
    if due_on is not None:
        item.due_on = due_on
    session.flush()


def add_checklist_item(session: Session, deal_id: int, text: str, user: User) -> ProcurementItem:
    _require(user)
    if not (text or "").strip():
        raise ProcurementError("Type what's needed.")
    count = session.scalar(select(func.count(ProcurementItem.id)).where(ProcurementItem.opportunity_id == deal_id))
    item = ProcurementItem(opportunity_id=deal_id, item=text.strip()[:200], sort_order=(count or 0) + 1)
    session.add(item)
    session.flush()
    return item


def deals_for_lead(session: Session, lead_id: int) -> list[Opportunity]:
    return list(session.scalars(
        select(Opportunity).where(Opportunity.lead_id == lead_id)
        .options(selectinload(Opportunity.line_items).selectinload(OpportunityProduct.product))
        .order_by(Opportunity.status != "open", Opportunity.created_at.desc())))


def checklist(session: Session, deal_id: int) -> list[ProcurementItem]:
    return list(session.scalars(select(ProcurementItem).where(ProcurementItem.opportunity_id == deal_id)
                                .order_by(ProcurementItem.sort_order, ProcurementItem.id)))


# ---------------------------------------------------------------- quotes


SECTION_OF_CATEGORY = {"simulator": "software", "kit": "hardware"}
SECTION_ORDER = ("software", "hardware", "other")


@dataclass
class LineIn:
    description: str
    quantity: int
    unit_price: Decimal
    gst_pct: Decimal = Decimal("18")
    details: str | None = None
    section: str = "other"
    unit: str | None = None
    hsn_sac: str | None = None
    price_item_id: int | None = None
    product_id: int | None = None


@dataclass
class QuoteDraft:
    lines: list[LineIn]
    price_basis: str = "institute"
    discount_pct: Decimal = Decimal("0")
    valid_days: int | None = None
    contact_id: int | None = None
    terms: str | None = None
    notes: str | None = None
    # Cover page; blanks get sensible defaults (title from settings, attention = designation, etc.).
    title: str | None = None
    attention: str | None = None
    greeting: str | None = None
    prepared_by: str | None = None
    ref_code: str | None = None   # the institution part of the reference, e.g. ALU


@dataclass(frozen=True)
class Totals:
    subtotal: Decimal
    discount: Decimal
    taxable: Decimal
    gst: Decimal
    total: Decimal
    gst_by_rate: dict


def compute_totals(lines: list[LineIn], discount_pct) -> Totals:
    """Discount applies to every line in proportion; GST is worked out per line on the discounted amount."""
    pct = to_decimal(discount_pct)
    subtotal = paise(sum((to_decimal(line.unit_price) * line.quantity for line in lines), Decimal("0")))
    discount = paise(subtotal * pct / 100)
    gst_total = Decimal("0")
    by_rate: dict[Decimal, Decimal] = {}
    for line in lines:
        amount = to_decimal(line.unit_price) * line.quantity
        taxable = amount * (1 - pct / 100)
        gst = paise(taxable * to_decimal(line.gst_pct) / 100)
        gst_total += gst
        rate = to_decimal(line.gst_pct).normalize()
        by_rate[rate] = by_rate.get(rate, Decimal("0")) + gst
    taxable_total = subtotal - discount
    return Totals(subtotal, discount, taxable_total, paise(gst_total), paise(taxable_total + gst_total), by_rate)


def period_start_of_fy(day: date) -> date:
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def fy_label(day: date) -> str:
    """Indian financial year, e.g. 20 Oct 2026 → '2026-27'; 15 Feb 2027 → '2026-27'."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def institution_code(name: str | None) -> str:
    """'Sahyadri Valley University' → 'SVU'; editable on each quote."""
    skip = {"of", "and", "the", "for", "&", "-"}
    words = [w for w in re.split(r"[\s,.()]+", name or "") if w and w.lower() not in skip]
    return ("".join(w[0] for w in words[:4]).upper() or "INST")[:8]


def next_quote_number(session: Session, prefix: str, on: date, code: str = "INST") -> str:
    """DEMO/SVU/2026-27/001 — the running number is per financial year across all institutions."""
    fy = fy_label(on)
    code = re.sub(r"[^A-Z0-9-]", "", (code or "INST").upper())[:12] or "INST"
    numbers = session.scalars(select(Quote.number).where(Quote.number.like(f"{prefix}/%/{fy}/%"))).all()
    used = [int(n.rsplit("/", 1)[-1]) for n in numbers if n.rsplit("/", 1)[-1].isdigit()]
    return f"{prefix}/{code}/{fy}/{(max(used) + 1 if used else 1):03d}"


def lines_from_price_items(session: Session, picks: list[tuple[int, int]], basis: str) -> list[LineIn]:
    """(price_item_id, quantity) → quote lines at the chosen price basis. Refuses items without that price."""
    lines = []
    for pid, qty in picks:
        item = session.get(PriceItem, int(pid))
        if item is None:
            raise ProcurementError("A price-list item no longer exists — refresh and pick again.")
        price = item.partner_price_inr if basis == "partner" else item.institute_price_inr
        if price is None:
            raise ProcurementError(f"'{item.name}' has no {basis} price yet — add it on Configuration → Price list.")
        section = SECTION_OF_CATEGORY.get(item.product.category, "other") if item.product else "other"
        lines.append(LineIn(description=item.name, quantity=max(int(qty), 1), unit_price=to_decimal(price),
                            gst_pct=to_decimal(item.gst_pct), unit=item.unit, hsn_sac=item.hsn_sac,
                            price_item_id=item.id, product_id=item.product_id, details=item.description,
                            section=section))
    return lines


def lines_from_bundle(session: Session, bundle_id: int, basis: str, multiplier: int = 1) -> list[LineIn]:
    bundle = session.get(Bundle, bundle_id)
    if bundle is None:
        raise ProcurementError("That bundle no longer exists.")
    return lines_from_price_items(session, [(bi.price_item_id, bi.quantity * max(multiplier, 1))
                                            for bi in bundle.items], basis)


def _validate(draft: QuoteDraft) -> None:
    if not draft.lines:
        raise ProcurementError("Add at least one item to the quote.")
    for i, line in enumerate(draft.lines, start=1):
        if not (line.description or "").strip():
            raise ProcurementError(f"Item {i} needs a description.")
        if line.quantity < 1:
            raise ProcurementError(f"Item {i}: quantity must be at least 1.")
        if to_decimal(line.unit_price) < 0:
            raise ProcurementError(f"Item {i}: price can't be negative.")
        if not Decimal("0") <= to_decimal(line.gst_pct) <= Decimal("100"):
            raise ProcurementError(f"Item {i}: GST % must be between 0 and 100.")
    if not Decimal("0") <= to_decimal(draft.discount_pct) <= Decimal("100"):
        raise ProcurementError("Discount must be between 0 and 100%.")
    if draft.price_basis not in ("institute", "partner"):
        raise ProcurementError("Pick institute or partner prices.")


def _fill(session: Session, quote: Quote, draft: QuoteDraft, settings: dict[str, str]) -> None:
    _validate(draft)
    deal = session.get(Opportunity, quote.opportunity_id)
    lead = session.get(Lead, deal.lead_id)
    contact = session.get(Contact, draft.contact_id or lead.primary_contact_id) \
        if (draft.contact_id or lead.primary_contact_id) else None
    totals = compute_totals(draft.lines, draft.discount_pct)
    quote.price_basis, quote.discount_pct = draft.price_basis, paise(draft.discount_pct)
    quote.subtotal_inr, quote.discount_inr, quote.gst_inr, quote.total_inr = (totals.subtotal, totals.discount,
                                                                               totals.gst, totals.total)
    days = draft.valid_days if draft.valid_days else _int_setting(settings, "quote_validity_days", 30)
    quote.valid_until = quote.quote_date + timedelta(days=days)
    quote.contact_id = contact.id if contact else None
    quote.to_name = contact.name if contact else None
    quote.to_designation = contact.designation if contact else None
    quote.to_institution = lead.organisation.name
    quote.terms = draft.terms or None
    quote.notes = (draft.notes or "").strip() or None
    creator = session.get(User, quote.created_by_user_id) if quote.created_by_user_id else None
    title = settings.get(f"title:{creator.email.lower()}", "") if creator else ""
    quote.title = (draft.title or "").strip() or settings.get("quote_title") or "Quotation"
    quote.attention = (draft.attention or "").strip() or (contact.designation if contact and contact.designation
                                                          else (contact.name if contact else None))
    quote.greeting = (draft.greeting or "").strip() or default_greeting(contact)
    quote.prepared_by = (draft.prepared_by or "").strip() or (
        ", ".join(x for x in (creator.name if creator else "", title) if x) or None)
    quote.lines = [QuoteLine(description=line.description.strip()[:400], details=(line.details or "").strip() or None,
                             section=line.section if line.section in SECTION_ORDER else "other",
                             quantity=line.quantity, unit_price_inr=paise(line.unit_price),
                             gst_pct=paise(line.gst_pct), unit=line.unit, hsn_sac=line.hsn_sac,
                             price_item_id=line.price_item_id, product_id=line.product_id, sort_order=i)
                   for i, line in enumerate(draft.lines, start=1)]


def default_greeting(contact: Contact | None) -> str:
    from app.services.library import split_name  # same name handling as the script library

    salutation, _first, last = split_name(contact.name if contact else "")
    if salutation and (last or _first):
        return f"Dear {salutation.rstrip('.')}. {last or _first},".replace("Prof..", "Prof.")
    return "Dear Sir/Madam,"


def create_quote(session: Session, deal_id: int, draft: QuoteDraft, user: User, *,
                 on: date | None = None) -> Quote:
    _require(user)
    deal = session.get(Opportunity, deal_id)
    if deal is None:
        raise ProcurementError("That deal no longer exists.")
    settings = get_settings(session)
    on = on or today_ist()
    lead = session.get(Lead, deal.lead_id)
    code = draft.ref_code or institution_code(lead.organisation.name)
    quote = Quote(opportunity_id=deal.id, number=next_quote_number(session, settings.get("quote_prefix") or "DEMO",
                                                                   on, code),
                  version=1, status="draft", quote_date=on, valid_until=on, created_by_user_id=user.id)
    session.add(quote)
    _fill(session, quote, draft, settings)
    session.flush()
    _audit(session, user, "quotes", quote.id, "create", {"Quote": [None, f"{quote.number} v1"]})
    return quote


def update_quote(session: Session, quote_id: int, draft: QuoteDraft, user: User) -> Quote:
    _require(user)
    quote = session.get(Quote, quote_id)
    if quote is None:
        raise ProcurementError("That quote no longer exists.")
    if quote.status != "draft":
        raise ProcurementError("Only drafts can be edited — make a revision instead.")
    _fill(session, quote, draft, get_settings(session))
    session.flush()
    return quote


def revise_quote(session: Session, quote_id: int, draft: QuoteDraft, user: User, *, on: date | None = None) -> Quote:
    """A new version under the same number (v2, v3…). The older one is superseded when the new one is sent."""
    _require(user)
    old = session.get(Quote, quote_id)
    if old is None:
        raise ProcurementError("That quote no longer exists.")
    latest = session.scalar(select(func.max(Quote.version)).where(Quote.number == old.number)) or old.version
    on = on or today_ist()
    quote = Quote(opportunity_id=old.opportunity_id, number=old.number, version=latest + 1, status="draft",
                  quote_date=on, valid_until=on, created_by_user_id=user.id, drive_url=None)
    session.add(quote)
    _fill(session, quote, draft, get_settings(session))
    session.flush()
    _audit(session, user, "quotes", quote.id, "create", {"Quote": [None, f"{quote.number} v{quote.version}"]})
    return quote


def draft_from_quote(quote: Quote) -> QuoteDraft:
    """Starting point for editing or revising: the quote's own lines and settings."""
    days = (quote.valid_until - quote.quote_date).days
    return QuoteDraft(
        lines=[LineIn(description=line.description, quantity=line.quantity, unit_price=line.unit_price_inr,
                      gst_pct=line.gst_pct, unit=line.unit, hsn_sac=line.hsn_sac, price_item_id=line.price_item_id,
                      product_id=line.product_id, details=line.details, section=line.section)
               for line in quote.lines],
        price_basis=quote.price_basis, discount_pct=quote.discount_pct, valid_days=days, contact_id=quote.contact_id,
        terms=quote.terms, notes=quote.notes, title=quote.title, attention=quote.attention, greeting=quote.greeting,
        prepared_by=quote.prepared_by)


def _type(session: Session, name: str) -> ActivityType:
    return session.scalar(select(ActivityType).where(ActivityType.name == name))


def mark_quote_sent(session: Session, quote_id: int, user: User, *, on: date | None = None) -> list[str]:
    _require(user)
    quote = session.get(Quote, quote_id)
    if quote is None or quote.status != "draft":
        raise ProcurementError("Only a draft can be marked as sent.")
    on = on or today_ist()
    settings = get_settings(session)
    deal = session.get(Opportunity, quote.opportunity_id)
    lead = session.get(Lead, deal.lead_id)
    messages = [f"Quote {quote.number} v{quote.version} marked as sent."]
    for older in session.scalars(select(Quote).where(Quote.number == quote.number, Quote.id != quote.id,
                                                     Quote.status.in_(OPEN_QUOTES))):
        older.status = "superseded"
        messages.append(f"v{older.version} superseded.")
    quote.status, quote.sent_on = "sent", on
    deal.value_inr = quote.subtotal_inr - quote.discount_inr  # deal values are excluding GST

    # On the timeline as an email we sent (also counts as outreach, and as answering them if they wrote first).
    email = Activity(activity_type=_type(session, "Email"), lead=lead, org_id=lead.org_id,
                     title=f"Quotation {quote.number} v{quote.version} sent"[:200], status="planned",
                     scheduled_at=datetime.now(IST), direction="outbound", owner_user_id=user.id,
                     notes=f"{inr(quote.subtotal_inr - quote.discount_inr)} excl. GST · valid until "
                           f"{quote.valid_until:%d %b %Y}",
                     created_by_user_id=user.id)
    if quote.contact_id:
        email.participants.append(ActivityParticipant(contact_id=quote.contact_id))
    session.add(email)
    session.flush()
    messages += complete_activity(session, email, user)
    if advance_to(session, lead, "Sent Quotation", user, why=f"quote {quote.number} sent"):
        messages.append("Stage moved to Sent Quotation.")

    remind_days = _int_setting(settings, "quote_expiry_reminder_days", 5)
    remind_on = max(quote.valid_until - timedelta(days=remind_days), on + timedelta(days=1))
    if remind_on <= quote.valid_until:
        reminder = Activity(activity_type=_type(session, "Follow-up task"), lead=lead, org_id=lead.org_id,
                            title=f"Quote {quote.number} expires {quote.valid_until:%d %b} — follow up"[:200],
                            status="planned", scheduled_at=at_ist(remind_on, time(10, 0)), direction="outbound",
                            owner_user_id=lead.owner_user_id or user.id, created_by_user_id=user.id,
                            notes=f"quote:{quote.id}")
        if quote.contact_id:
            reminder.participants.append(ActivityParticipant(contact_id=quote.contact_id))
        session.add(reminder)
        messages.append(f"Reminder planned for {remind_on:%a %d %b}.")
    _audit(session, user, "quotes", quote.id, "update", {"Status": ["draft", "sent"]})
    session.flush()
    return messages


def _cancel_reminders(session: Session, quote: Quote, why: str) -> None:
    for task in session.scalars(select(Activity).where(Activity.status == "planned",
                                                       Activity.notes == f"quote:{quote.id}")):
        task.status, task.outcome = "cancelled", why


def accept_quote(session: Session, quote_id: int, po_date: date, user: User) -> list[str]:
    """PO received: the deal is won, the lead moves to the won stage, and delivery follow-ups are planned."""
    _require(user)
    quote = session.get(Quote, quote_id)
    if quote is None or quote.status not in ("sent", "expired"):
        raise ProcurementError("Only a sent (or expired) quote can be accepted.")
    deal = session.get(Opportunity, quote.opportunity_id)
    lead = session.get(Lead, deal.lead_id)
    quote.status, quote.accepted_on = "accepted", po_date
    deal.status, deal.po_date, deal.expected_close = "won", po_date, po_date
    deal.value_inr = quote.subtotal_inr - quote.discount_inr
    _cancel_reminders(session, quote, "Quote accepted")
    messages = [f"Quote {quote.number} v{quote.version} accepted — PO dated {po_date:%d %b %Y}."]

    quantities: dict[int, int] = {}
    for line in quote.lines:
        if line.product_id:
            quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
    existing = {item.product_id: item for item in deal.line_items}
    for pid, qty in quantities.items():
        product = session.get(Product, pid)
        due = po_date + timedelta(days=product.delivery_days or 0)
        item = existing.get(pid)
        if item is None:
            deal.line_items.append(OpportunityProduct(product_id=pid, quantity=qty, expected_delivery_on=due))
        else:
            item.quantity, item.expected_delivery_on = qty, due
        if due >= today_ist():
            session.add(Activity(activity_type=_type(session, "Follow-up task"), lead=lead, org_id=lead.org_id,
                                 title=f"Delivery due: {product.name} — {lead.organisation.name}"[:200],
                                 status="planned", scheduled_at=at_ist(due, time(10, 0)), direction="internal",
                                 owner_user_id=lead.owner_user_id or user.id, created_by_user_id=user.id))
            messages.append(f"Delivery follow-up: {product.name} on {due:%d %b %Y}.")
    won = session.scalar(select(Stage).where(Stage.outcome == "won", Stage.is_active.is_(True))
                         .order_by(Stage.sort_order))
    if won is not None and advance_to(session, lead, won.name, user, why=f"PO for quote {quote.number}"):
        messages.append(f"Stage moved to {won.name}.")
    _audit(session, user, "quotes", quote.id, "update", {"Status": ["sent", "accepted"], "PO date": [None, str(po_date)]})
    session.flush()
    from app.services.postsale import ensure_installation  # local: post-sale imports tasks, which imports cadence

    ensure_installation(session, deal.id)
    messages.append("Client record started (Clients page) for installation, training, warranty and AMC.")
    return messages


def cancel_quote(session: Session, quote_id: int, user: User) -> None:
    _require(user)
    quote = session.get(Quote, quote_id)
    if quote is None or quote.status not in OPEN_QUOTES:
        raise ProcurementError("Only drafts and sent quotes can be cancelled.")
    old, quote.status = quote.status, "cancelled"
    _cancel_reminders(session, quote, "Quote cancelled")
    _audit(session, user, "quotes", quote.id, "update", {"Status": [old, "cancelled"]})
    session.flush()


def set_quote_link(session: Session, quote_id: int, url: str | None, user: User) -> None:
    from app.services.library import URL_RE  # same rule as the Library

    _require(user)
    quote = session.get(Quote, quote_id)
    url = (url or "").strip()
    if url and not URL_RE.fullmatch(url):
        raise ProcurementError("Paste a full link starting with https://")
    quote.drive_url = url or None
    session.flush()


def expire_quotes(session: Session, today: date | None = None) -> int:
    """Sent quotes past their validity become 'expired' (run once a day with the other daily checks)."""
    today = today or today_ist()
    expired = list(session.scalars(select(Quote).where(Quote.status == "sent", Quote.valid_until < today)))
    for quote in expired:
        quote.status = "expired"
    session.flush()
    return len(expired)


def quotes_for_deal(session: Session, deal_id: int) -> list[Quote]:
    return list(session.scalars(select(Quote).where(Quote.opportunity_id == deal_id)
                                .options(selectinload(Quote.lines))
                                .order_by(Quote.number.desc(), Quote.version.desc())))


def quote_with_lines(session: Session, quote_id: int) -> Quote | None:
    return session.scalar(select(Quote).where(Quote.id == quote_id).options(selectinload(Quote.lines)))


def quotes_frame(session: Session, today: date | None = None) -> pd.DataFrame:
    today = today or today_ist()
    rows = session.execute(
        select(Quote, Opportunity.title, Lead.id, Lead.potential, User.name)
        .join(Opportunity, Opportunity.id == Quote.opportunity_id)
        .join(Lead, Lead.id == Opportunity.lead_id)
        .outerjoin(User, User.id == Quote.created_by_user_id)
        .order_by(Quote.quote_date.desc(), Quote.number.desc(), Quote.version.desc())
    ).all()
    return pd.DataFrame([{
        "id": q.id, "lead_id": lead_id, "Quote": f"{q.number} v{q.version}", "Institution": q.to_institution or "",
        "Deal": deal, "Status": q.status.title(), "Value excl. GST": float(q.subtotal_inr - q.discount_inr),
        "Total incl. GST": float(q.total_inr), "Basis": q.price_basis.title(),
        "Date": q.quote_date, "Valid until": q.valid_until,
        "Days left": (q.valid_until - today).days if q.status == "sent" else None,
        "Potential": potential or "", "By": by or "",
    } for q, deal, lead_id, potential, by in rows],
        columns=["id", "lead_id", "Quote", "Institution", "Deal", "Status", "Value excl. GST", "Total incl. GST",
                 "Basis", "Date",
                 "Valid until", "Days left", "Potential", "By"])


def pipeline_value(frame: pd.DataFrame) -> tuple[float, float]:
    """Open (sent) quotes, excluding GST: total, and total weighted by each lead's Potential."""
    sent = frame[frame["Status"] == "Sent"] if not frame.empty else frame
    if sent.empty:
        return 0.0, 0.0
    weights = sent["Potential"].map(lambda p: POTENTIAL_WEIGHT.get(p, 0.5))
    return float(sent["Value excl. GST"].sum()), float((sent["Value excl. GST"] * weights).sum())


# ---------------------------------------------------------------- the Word quotation


def section_texts(settings: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {key: (settings.get(f"section_{key}_title") or key.title(), settings.get(f"section_{key}_intro") or "")
            for key in SECTION_ORDER}


def quote_docx(session: Session, quote_id: int) -> tuple[bytes, str]:
    """The quotation as a Word file from the team's template, plus a file name."""
    from app.services.quote_docx import Section, build_quote_docx, money, template_bytes

    quote = quote_with_lines(session, quote_id)
    if quote is None:
        raise ProcurementError("That quote no longer exists.")
    settings = get_settings(session)
    texts = section_texts(settings)
    grouped: dict[str, list[dict]] = {}
    for line in quote.lines:
        amount = paise(line.unit_price_inr * line.quantity)
        grouped.setdefault(line.section if line.section in SECTION_ORDER else "other", []).append({
            "name": line.description, "details": line.details or "", "qty": line.quantity,
            "unit_price": money(line.unit_price_inr), "amount": money(amount), "raw_amount": amount})
    sections = [(Section(key, *texts[key]), grouped[key]) for key in SECTION_ORDER if key in grouped]
    taxable = quote.subtotal_inr - quote.discount_inr
    summary = None
    if settings.get("quote_show_summary", "yes").lower() in ("yes", "true", "1") and (
            len(sections) > 1 or quote.discount_inr > 0):
        rates = sorted({paise(line.gst_pct) for line in quote.lines})
        gst_label = "GST @ " + ", ".join(f"{r.normalize():f}%" for r in rates) if rates else "GST"
        summary = [("Subtotal (excluding GST)", money(quote.subtotal_inr), False)]
        if quote.discount_inr > 0:
            summary.append((f"Discount ({quote.discount_pct.normalize():f}%)", "-" + money(quote.discount_inr), False))
            summary.append(("Total after discount (excluding GST)", money(taxable), True))
        summary += [(gst_label, money(quote.gst_inr), False), ("Total including GST", money(quote.total_inr), True)]
    days = (quote.valid_until - quote.quote_date).days
    values = {
        "title": quote.title or "Quotation", "submitted_to": quote.to_institution or "",
        "attention": quote.attention or "", "reference": f"{quote.number}" + (f" (v{quote.version})"
                                                                             if quote.version > 1 else ""),
        "date": quote.quote_date.strftime("%d/%m/%Y"), "validity": f"{days} days from date of issue",
        "valid_days": str(days), "prepared_by": quote.prepared_by or "", "greeting": quote.greeting or "Dear Sir/Madam,",
        "institution": quote.to_institution or "your institution",
        "signature_name": (quote.prepared_by or "").split(",")[0],
        "signature_title": ", ".join(x.strip() for x in (quote.prepared_by or "").split(",")[1:]),
    }
    data = build_quote_docx(values=values, sections=sections, summary=summary, template=template_bytes(settings))
    safe = re.sub(r"[^A-Za-z0-9-]+", "_", f"Quotation {quote.number} v{quote.version} {quote.to_institution or ''}")
    return data, safe.strip("_")[:120] + ".docx"


# ---------------------------------------------------------------- checklist rules (Configuration)


def rules_frame(session: Session) -> pd.DataFrame:
    return pd.DataFrame([{"id": r.id, "Buying route": r.buying_route, "Document needed": r.item,
                          "Only for reseller deals": r.only_if_reseller, "Active": r.is_active}
                         for r in session.scalars(select(ProcurementRule).order_by(ProcurementRule.sort_order,
                                                                                    ProcurementRule.id))],
                        columns=["id", "Buying route", "Document needed", "Only for reseller deals", "Active"])


def save_rules(session: Session, frame: pd.DataFrame, user: User) -> int:
    """Upserts checklist rules from the editable table. Rows are never deleted (untick Active instead), so deals that
    already have the item keep it."""
    _require(user, "admin")
    existing = {r.id: r for r in session.scalars(select(ProcurementRule))}
    changed = 0
    for order, rec in enumerate(frame.to_dict("records"), start=1):
        route, item = str(rec.get("Buying route") or "").strip(), str(rec.get("Document needed") or "").strip()
        if not route or not item:
            continue
        rid = rec.get("id")
        row = existing.get(int(rid)) if rid is not None and not pd.isna(rid) else None
        values = dict(buying_route=route[:80], item=item[:200], only_if_reseller=bool(rec.get("Only for reseller deals")),
                      is_active=rec.get("Active") is not False, sort_order=order)
        if row is None:
            session.add(ProcurementRule(**values))
            changed += 1
        elif any(getattr(row, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(row, k, v)
            changed += 1
    if changed:
        _audit(session, user, "procurement_rules", "-", "update", {"Rules": [None, f"{changed} change(s)"]})
    session.flush()
    return changed
