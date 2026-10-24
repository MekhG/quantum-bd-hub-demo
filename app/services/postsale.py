"""Post-sale / installed base: one record per won deal — delivery, installation, acceptance, product training,
warranty, AMC, the next purchase and upsell — with reminders on Today.

Created automatically when a PO is received (and back-filled for deals won earlier). Warranty runs
`warranty_months` (12) from acceptance; reminders go out `warranty_reminder_days` (60) before it ends and
`amc_reminder_days` (45) before an AMC ends. The suggested AMC follows the team's quotation: support from year 2 at
6% / 8% / 10% / 12% of equipment value (`amc_rates`).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    AuditLog,
    Installation,
    Lead,
    Opportunity,
    OpportunityProduct,
    Organisation,
    Product,
    User,
)
from app.models.partners import AMC_STATUSES
from app.services.cadence import today_ist
from app.services.tasks import cancel_tagged_tasks, plan_tagged_task

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
DATE_FIELDS = ("delivered_on", "installed_on", "accepted_on", "training_on", "amc_start", "amc_end", "upsell_on")


class PostSaleError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _setting(session: Session, key: str, default: str) -> str:
    row = session.get(AppSetting, key)
    return row.value if row and row.value else default


def _int_setting(session: Session, key: str, default: int) -> int:
    try:
        return int(_setting(session, key, str(default)))
    except ValueError:
        return default


def ensure_installation(session: Session, opportunity_id: int) -> Installation:
    inst = session.scalar(select(Installation).where(Installation.opportunity_id == opportunity_id))
    if inst is None:
        inst = Installation(opportunity_id=opportunity_id, warranty_months=_int_setting(session, "warranty_months", 12))
        session.add(inst)
        session.flush()
    return inst


def sync_installations(session: Session) -> int:
    """Creates the record for every won deal that doesn't have one yet (daily, and after a PO)."""
    missing = list(session.scalars(select(Opportunity.id).where(
        Opportunity.status == "won", ~select(Installation.id).where(
            Installation.opportunity_id == Opportunity.id).exists())))
    for opportunity_id in missing:
        ensure_installation(session, opportunity_id)
    return len(missing)


def warranty_end(accepted_on: date | None, months: int) -> date | None:
    return accepted_on + relativedelta(months=months) - timedelta(days=1) if accepted_on else None


def suggested_amc(session: Session, opportunity_id: int) -> list[dict]:
    """Support from year 2, as in the quotation: rate × the deal's equipment value (excl. GST)."""
    deal = session.get(Opportunity, opportunity_id)
    value = float(deal.value_inr or 0) if deal else 0.0
    rates = []
    for part in _setting(session, "amc_rates", "5,7,9,11").split(","):
        try:
            rates.append(float(part.strip()))
        except ValueError:
            continue
    return [{"Year": f"Year {i}", "Rate": f"{rate:g}%", "Suggested AMC (₹)": round(value * rate / 100)}
            for i, rate in enumerate(rates, start=2)]


def _parse_money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except InvalidOperation as error:
        raise PostSaleError("AMC value must be a number (₹).") from error


def save_installation(session: Session, installation_id: int, values: dict, user: User,
                      today: date | None = None) -> list[str]:
    """Saves the milestones and (re)plans the reminders. Returns what was planned, for the person."""
    _require(user)
    today = today or today_ist()
    inst = session.get(Installation, installation_id)
    if inst is None:
        raise PostSaleError("That client record no longer exists.")
    # Check the dates as they will be before changing anything, so a refused save leaves the record untouched.
    dates = {key: (values[key] or None) if key in values else getattr(inst, key) for key in DATE_FIELDS}
    if dates["installed_on"] and dates["delivered_on"] and dates["installed_on"] < dates["delivered_on"]:
        raise PostSaleError("Installation can't be before delivery.")
    if dates["accepted_on"] and dates["installed_on"] and dates["accepted_on"] < dates["installed_on"]:
        raise PostSaleError("Acceptance can't be before installation.")
    if dates["amc_end"] and dates["amc_start"] and dates["amc_end"] < dates["amc_start"]:
        raise PostSaleError("The AMC ends before it starts.")
    if "amc_status" in values and values["amc_status"] not in AMC_STATUSES:
        raise PostSaleError("Pick an AMC status.")
    amc_value = _parse_money(values["amc_value_inr"]) if "amc_value_inr" in values else inst.amc_value_inr
    for key, value in dates.items():
        setattr(inst, key, value)
    for key in ("training_done", "reference_ok", "case_study_ok"):
        if key in values:
            setattr(inst, key, bool(values[key]))
    if "warranty_months" in values and values["warranty_months"]:
        inst.warranty_months = int(values["warranty_months"])
    if "amc_status" in values:
        inst.amc_status = values["amc_status"]
    inst.amc_value_inr = amc_value
    for key in ("next_phase", "notes"):
        if key in values:
            setattr(inst, key, (values[key] or "").strip() or None)
    inst.warranty_end = warranty_end(inst.accepted_on, inst.warranty_months)
    session.add(AuditLog(user_id=user.id, entity="installations", entity_id=str(inst.id), action="update",
                         diff={"Installed base": [None, "milestones saved"]}))
    session.flush()
    return plan_reminders(session, inst, user, today)


def plan_reminders(session: Session, inst: Installation, user: User, today: date | None = None) -> list[str]:
    today = today or today_ist()
    deal = session.get(Opportunity, inst.opportunity_id)
    lead = session.get(Lead, deal.lead_id)
    org = lead.organisation.name
    owner = lead.owner_user_id or user.id
    planned: list[str] = []

    def plan(kind: str, title: str, day: date | None, note: str) -> None:
        tag = f"[client:{inst.id}:{kind}]"
        if day is None:
            cancel_tagged_tasks(session, tag)
            return
        task = plan_tagged_task(session, tag=tag, title=f"{title} — {org}"[:200], day=day, owner_id=owner,
                                user=user, lead_id=lead.id, note=note, today=today)
        if task is not None:
            planned.append(f"{title} on {day:%d %b %Y}")

    plan("training", "Product training", inst.training_on if not inst.training_done else None,
         "Demo product training for the faculty.")
    def ahead(end: date | None, days: int) -> date | None:
        # N days before the end; if that's already past but the end isn't, remind today rather than never.
        return None if end is None or end < today else max(end - timedelta(days=days), today)

    warn = _int_setting(session, "warranty_reminder_days", 60)
    plan("warranty", "Warranty ending — offer AMC", ahead(inst.warranty_end, warn)
         if inst.amc_status in ("none", "quoted") else None,
         f"Warranty ends {inst.warranty_end:%d %b %Y}." if inst.warranty_end else "")
    renew = _int_setting(session, "amc_reminder_days", 45)
    plan("amc", "AMC renewal", ahead(inst.amc_end, renew) if inst.amc_status == "active" else None,
         f"AMC ends {inst.amc_end:%d %b %Y}." if inst.amc_end else "")
    plan("upsell", f"Next phase: {inst.next_phase}" if inst.next_phase else "Upsell follow-up", inst.upsell_on,
         "Discuss the next purchase.")
    return planned


def next_milestone(inst: Installation, delivery_due: date | None, today: date) -> tuple[str, date | None, bool]:
    """The next thing to happen for a client: (what, when, overdue)."""
    steps = [
        ("Delivery", inst.delivered_on, delivery_due),
        ("Installation", inst.installed_on, None),
        ("Acceptance", inst.accepted_on, None),
        ("Product training", inst.training_on if inst.training_done else None, inst.training_on),
    ]
    for name, done, due in steps:
        if not done:
            return name, due, bool(due and due < today)
    if inst.warranty_end and inst.amc_status in ("none", "quoted"):
        return "Warranty ends", inst.warranty_end, inst.warranty_end < today
    if inst.amc_status == "active" and inst.amc_end:
        return "AMC renewal", inst.amc_end, inst.amc_end < today
    if inst.upsell_on:
        return "Upsell", inst.upsell_on, inst.upsell_on < today
    return "—", None, False


def installations_frame(session: Session, today: date | None = None) -> pd.DataFrame:
    today = today or today_ist()
    products: dict[int, list[str]] = {}
    due: dict[int, date] = {}
    for opp_id, name, expected in session.execute(
            select(OpportunityProduct.opportunity_id, Product.name, OpportunityProduct.expected_delivery_on)
            .join(Product, Product.id == OpportunityProduct.product_id)):
        products.setdefault(opp_id, []).append(name)
        if expected and (opp_id not in due or expected > due[opp_id]):
            due[opp_id] = expected
    rows = session.execute(
        select(Installation, Opportunity, Organisation.name, Lead.id)
        .join(Opportunity, Opportunity.id == Installation.opportunity_id).join(Lead, Lead.id == Opportunity.lead_id)
        .join(Organisation, Organisation.id == Lead.org_id).order_by(Opportunity.po_date.desc())).all()
    records = []
    for inst, deal, org, lead_id in rows:
        what, when, late = next_milestone(inst, due.get(deal.id), today)
        records.append({
            "id": inst.id, "opportunity_id": deal.id, "lead_id": lead_id, "Client": org, "Deal": deal.title,
            "PO date": deal.po_date, "Value (₹)": float(deal.value_inr or 0),
            "Products": ", ".join(products.get(deal.id, [])), "Delivery due": due.get(deal.id),
            "Delivered": inst.delivered_on, "Installed": inst.installed_on, "Accepted": inst.accepted_on,
            "Training": "Done" if inst.training_done else (inst.training_on.strftime("%d %b %Y")
                                                           if inst.training_on else ""),
            "Warranty ends": inst.warranty_end, "AMC": inst.amc_status.title(), "AMC ends": inst.amc_end,
            "Next phase": inst.next_phase or "", "Upsell on": inst.upsell_on,
            "Reference": inst.reference_ok, "Next": what, "When": when, "Overdue": late,
        })
    return pd.DataFrame(records, columns=[
        "id", "opportunity_id", "lead_id", "Client", "Deal", "PO date", "Value (₹)", "Products", "Delivery due",
        "Delivered", "Installed", "Accepted", "Training", "Warranty ends", "AMC", "AMC ends", "Next phase",
        "Upsell on", "Reference", "Next", "When", "Overdue"])


def renewals_due(session: Session, days: int = 90, today: date | None = None) -> int:
    """Warranties and AMCs ending within `days` — for the Dashboard."""
    today = today or today_ist()
    horizon = today + timedelta(days=days)
    return int(session.scalar(select(func.count(Installation.id)).where(
        ((Installation.warranty_end.between(today, horizon)) & Installation.amc_status.in_(("none", "quoted")))
        | ((Installation.amc_end.between(today, horizon)) & (Installation.amc_status == "active")))) or 0)
