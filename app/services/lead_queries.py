"""Read-only queries behind the Leads and Colleges pages, returned as DataFrames."""

from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ACADEMIC_ORG_TYPES,
    Activity,
    AuditLog,
    ActivityParticipant,
    ActivityType,
    Contact,
    ImportBatch,
    Lead,
    LostReason,
    Opportunity,
    OpportunityProduct,
    Organisation,
    Product,
    Stage,
    Track,
    User,
    lead_products,
)

from app.services.cadence import cadence_label, steps_per_track  # noqa: E402
from app.services.versions import version_series  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

ORG_TYPE_LABELS = {
    "college": "College",
    "standalone": "Standalone",
    "university": "University",
    "vendor": "Vendor",
    "state_org": "State org",
    "csr_company": "Company or foundation",
    "partner": "Partner",
    "individual": "Individual",
    "other": "Other",
}


def _ist(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).tz_convert(IST).tz_localize(None) if pd.Timestamp(value).tzinfo else value


def leads_frame(session: Session) -> pd.DataFrame:
    contacts_per_org = (
        select(Contact.org_id, func.count(Contact.id).label("n")).group_by(Contact.org_id).subquery()
    )
    last_done = (
        select(Activity.lead_id, func.max(Activity.occurred_at).label("last_at"))
        .where(Activity.status == "done")
        .group_by(Activity.lead_id)
        .subquery()
    )
    next_planned = (
        select(Activity.lead_id, func.min(Activity.scheduled_at).label("next_at"))
        .where(Activity.status == "planned")
        .group_by(Activity.lead_id)
        .subquery()
    )
    query = (
        select(
            Lead.id.label("lead_id"),
            Organisation.name.label("Institution"),
            Organisation.org_type,
            Organisation.state.label("State"),
            Organisation.city.label("City"),
            Organisation.tier.label("Tier"),
            Track.name.label("Track"),
            Stage.name.label("Stage"),
            Stage.sort_order.label("stage_order"),
            User.name.label("Owner"),
            Lead.potential.label("Potential"),
            Lead.score.label("Score"),
            Lead.channel.label("Channel"),
            Lead.interest_areas.label("Interest areas"),
            Lead.nqm_flag.label("NQM"),
            Lead.source.label("Source"),
            Lead.notes.label("Notes"),
            LostReason.name.label("Rejected reason"),
            Lead.track_id.label("track_id"),
            Lead.cadence_status.label("cadence_status"),
            Lead.current_step_no.label("cadence_step"),
            Lead.cadence_due_on.label("cadence_due"),
            Lead.stage_changed_at.label("In stage since"),
            Lead.org_id.label("org_id"),
            Lead.updated_at.label("_lead_updated"),
            Organisation.updated_at.label("_org_updated"),
            func.coalesce(contacts_per_org.c.n, 0).label("Contacts"),
            last_done.c.last_at.label("Last activity"),
            next_planned.c.next_at.label("Next planned"),
            Lead.created_at.label("Added"),
        )
        .join(Organisation, Organisation.id == Lead.org_id)
        .join(Track, Track.id == Lead.track_id)
        .join(Stage, Stage.id == Lead.stage_id)
        .outerjoin(User, User.id == Lead.owner_user_id)
        .outerjoin(LostReason, LostReason.id == Lead.lost_reason_id)
        .outerjoin(contacts_per_org, contacts_per_org.c.org_id == Lead.org_id)
        .outerjoin(last_done, last_done.c.lead_id == Lead.id)
        .outerjoin(next_planned, next_planned.c.lead_id == Lead.id)
        .order_by(Organisation.name)
    )
    frame = pd.DataFrame(session.execute(query).mappings().all())
    if frame.empty:
        return frame
    fits = pd.DataFrame(
        session.execute(
            select(lead_products.c.lead_id, Product.name).join(Product, Product.id == lead_products.c.product_id)
        ).all(),
        columns=["lead_id", "product"],
    )
    fit_text = fits.groupby("lead_id")["product"].agg(", ".join) if not fits.empty else pd.Series(dtype=str)
    frame["Product fit"] = frame["lead_id"].map(fit_text).fillna("")
    frame["Type"] = frame.pop("org_type").map(ORG_TYPE_LABELS)
    frame["_version"] = version_series(frame.pop("_lead_updated"), frame.pop("_org_updated"))
    totals = steps_per_track(session)
    frame["Cadence"] = [
        cadence_label(status, step, totals.get(track), due if isinstance(due, date) else None)
        for status, step, track, due in zip(frame["cadence_status"], frame["cadence_step"], frame["track_id"],
                                            frame["cadence_due"])
    ]
    for column in ("Last activity", "Next planned", "Added", "In stage since"):
        frame[column] = frame[column].map(_ist)
    return frame


def lead_detail(session: Session, lead_id: int) -> dict[str, Any] | None:
    lead = session.scalar(
        select(Lead)
        .where(Lead.id == lead_id)
        .options(
            selectinload(Lead.organisation),
            selectinload(Lead.product_fit),
            selectinload(Lead.opportunities).selectinload(Opportunity.line_items).selectinload(
                OpportunityProduct.product
            ),
        )
    )
    if lead is None:
        return None
    org = lead.organisation

    contacts = pd.DataFrame(
        [
            {
                "Name": c.name,
                "Contact type": c.contact_type or "",
                "Designation": c.designation or "",
                "Department": c.department or "",
                "Email": c.email or "",
                "Phone": c.phone or "",
                "Research interests": c.research_interests or "",
                "Verified": c.verified,
                "Do not contact": c.do_not_contact,
                "Notes": c.notes or "",
                "Main contact": c.id == lead.primary_contact_id,
            }
            for c in session.scalars(select(Contact).where(Contact.org_id == org.id).order_by(Contact.name))
        ]
    )

    # Timeline covers the whole institution, across tracks: "what was said last", newest first.
    rows = session.execute(
        select(
            Activity.id,
            func.coalesce(Activity.occurred_at, Activity.scheduled_at).label("when"),
            Activity.status,
            Activity.direction,
            ActivityType.name.label("type"),
            Activity.title,
            Activity.notes,
            User.name.label("owner"),
        )
        .join(ActivityType, ActivityType.id == Activity.type_id)
        .outerjoin(User, User.id == Activity.owner_user_id)
        .where(Activity.org_id == org.id)
        .order_by(func.coalesce(Activity.occurred_at, Activity.scheduled_at).desc())
    ).mappings().all()
    people = {
        activity_id: name
        for activity_id, name in session.execute(
            select(ActivityParticipant.activity_id, Contact.name)
            .join(Contact, Contact.id == ActivityParticipant.contact_id)
            .where(ActivityParticipant.activity_id.in_([r["id"] for r in rows] or [0]))
        )
    }
    timeline = pd.DataFrame(
        [
            {
                "When (IST)": _ist(r["when"]),
                "Type": r["type"],
                "Status": r["status"],
                "Direction": r["direction"],
                "With": people.get(r["id"], ""),
                "Title": r["title"],
                "Notes": r["notes"] or "",
                "Owner": r["owner"] or "",
            }
            for r in rows
        ]
    )

    deals = pd.DataFrame(
        [
            {
                "Deal": o.title,
                "Status": o.status,
                "PO date": o.po_date,
                "Product": item.product.name,
                "Delivery due": item.expected_delivery_on,
                "Delivered": item.delivered_on,
            }
            for o in lead.opportunities
            for item in o.line_items
        ]
    )
    other_tracks = [
        name
        for name, in session.execute(
            select(Track.name).join(Lead, Lead.track_id == Track.id).where(Lead.org_id == org.id, Lead.id != lead.id)
        )
    ]
    return {
        "org": org,
        "lead": lead,
        "contacts": contacts,
        "timeline": timeline,
        "deals": deals,
        "product_fit": [p.name for p in lead.product_fit],
        "other_tracks": other_tracks,
    }


def colleges_frame(session: Session) -> pd.DataFrame:
    orgs = pd.DataFrame(
        session.execute(
            select(
                Organisation.id.label("org_id"),
                Organisation.name.label("Name"),
                Organisation.org_type,
                Organisation.aishe_code.label("AISHE Code"),
                Organisation.state.label("State"),
                Organisation.city.label("Location"),
                Organisation.affiliated_university.label("University Name"),
                Organisation.website.label("Website"),
                Organisation.tier.label("Tier"),
                Organisation.area.label("Area"),
                Organisation.management.label("Management"),
                Organisation.category.label("Category"),
                Organisation.source_lists.label("Lists"),
                Organisation.updated_at.label("_updated"),
            ).where(Organisation.org_type.in_(ACADEMIC_ORG_TYPES))
            .order_by(Organisation.state, Organisation.name)
        ).mappings().all()
    )
    if orgs.empty:
        return orgs
    tracks = pd.DataFrame(
        session.execute(select(Lead.org_id, Track.name).join(Track, Track.id == Lead.track_id)).all(),
        columns=["org_id", "track"],
    )
    contacts = pd.DataFrame(
        session.execute(select(Contact.org_id, func.count(Contact.id)).group_by(Contact.org_id)).all(),
        columns=["org_id", "n"],
    )
    track_text = tracks.groupby("org_id")["track"].agg(", ".join) if not tracks.empty else pd.Series(dtype=str)
    orgs["Lead in tracks"] = orgs["org_id"].map(track_text).fillna("")
    orgs["Contacts"] = orgs["org_id"].map(contacts.set_index("org_id")["n"] if not contacts.empty else {}).fillna(0)
    orgs["Contacts"] = orgs["Contacts"].astype(int)
    orgs["Type"] = orgs.pop("org_type").map(ORG_TYPE_LABELS)
    orgs["_version"] = version_series(orgs.pop("_updated"))
    return orgs


# The team's tracker columns, in their order, so a research batch drops straight into the AI workflow
# and imports back without any column changes.
RESEARCH_COLUMNS = [
    "Owner", "Status", "Tier", "Potential", "Location", "Type (College/ Standalone/University)",
    "Institution Name", "Contact Name", "Email Id", "Contact Number", "Contact Type", "Department",
    "Faculty Research Interests (If Faculty)", "Product Fit (If any)", "Channel",
    "AISHE Code", "Website", "State", "University Name",
]


def research_sheet(colleges: pd.DataFrame) -> bytes:
    rows = []
    for _, c in colleges.iterrows():
        location = ", ".join(x for x in [c.get("Location"), c.get("State")] if isinstance(x, str) and x)
        rows.append({
            "Tier": c.get("Tier") or "",
            "Location": location,
            "Type (College/ Standalone/University)": c.get("Type") or "",
            "Institution Name": c["Name"],
            "AISHE Code": c.get("AISHE Code") or "",
            "Website": c.get("Website") or "",
            "State": c.get("State") or "",
            "University Name": c.get("University Name") or "",
        })
    frame = pd.DataFrame(rows, columns=RESEARCH_COLUMNS).fillna("")
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Research batch", index=False)
        sheet = writer.sheets["Research batch"]
        for column_cells in sheet.columns:
            sheet.column_dimensions[column_cells[0].column_letter].width = 22
        sheet.freeze_panes = "A2"
    return buffer.getvalue()


def import_history(session: Session, limit: int = 50) -> pd.DataFrame:
    rows = session.execute(
        select(ImportBatch, User.name, Track.name)
        .outerjoin(User, User.id == ImportBatch.imported_by_user_id)
        .outerjoin(Track, Track.id == ImportBatch.track_id)
        .order_by(ImportBatch.imported_at.desc())
        .limit(limit)
    ).all()
    return pd.DataFrame(
        [
            {
                "ID": batch.id,
                "When (IST)": _ist(batch.imported_at),
                "Kind": batch.kind,
                "File": batch.file_name + (f" › {batch.sheet_name}" if batch.sheet_name else ""),
                "Track": track or "",
                "By": user or "",
                "Rows": batch.rows_total,
                "Result": _summary_text(batch.summary),
                "Undone": _ist(batch.undone_at),
            }
            for batch, user, track in rows
        ]
    )


def _summary_text(summary: Any) -> str:
    # Lead imports store [label, value] pairs; the community import stores ready-made "label: value" strings.
    pairs = summary.items() if isinstance(summary, dict) else (summary or [])
    return ", ".join(item if isinstance(item, str) else f"{item[0]}: {item[1]}" for item in pairs)


def frame_to_xlsx(frame: pd.DataFrame, sheet_name: str) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return buffer.getvalue()


def stage_list(session: Session) -> pd.DataFrame:
    """Stages in pipeline order with their outcome; used to colour stage pills."""
    rows = session.execute(select(Stage.name, Stage.outcome, Stage.sort_order).order_by(Stage.sort_order)).all()
    return pd.DataFrame(rows, columns=["name", "outcome", "sort_order"])


def contacts_frame(session: Session) -> pd.DataFrame:
    """Every contact with its institution and the tracks that institution is a lead in."""
    frame = pd.DataFrame(
        session.execute(
            select(
                Contact.id.label("contact_id"),
                Contact.name.label("Name"),
                Organisation.name.label("Institution"),
                Organisation.state.label("State"),
                Contact.contact_type.label("Contact type"),
                Contact.designation.label("Designation"),
                Contact.department.label("Department"),
                Contact.email.label("Email"),
                Contact.phone.label("Phone"),
                Contact.linkedin_url.label("LinkedIn"),
                Contact.research_interests.label("Research interests"),
                Contact.verified.label("Verified"),
                Contact.email_status.label("Email status"),
                Contact.do_not_contact.label("Do not contact"),
                Contact.notes.label("Notes"),
                Contact.source.label("Source"),
                Contact.org_id.label("org_id"),
                Contact.created_at.label("Added"),
                Contact.updated_at.label("_updated"),
            )
            .outerjoin(Organisation, Organisation.id == Contact.org_id)
            .order_by(Organisation.name, Contact.name)
        ).mappings().all()
    )
    if frame.empty:
        return frame
    tracks = pd.DataFrame(
        session.execute(select(Lead.org_id, Track.name).join(Track, Track.id == Lead.track_id)).all(),
        columns=["org_id", "track"],
    )
    track_text = tracks.groupby("org_id")["track"].agg(", ".join) if not tracks.empty else pd.Series(dtype=str)
    frame["Tracks"] = frame["org_id"].map(track_text).fillna("")
    frame["_version"] = version_series(frame.pop("_updated"))
    frame["Added"] = frame["Added"].map(_ist)
    return frame


def audit_frame(session: Session, limit: int = 500, entity: str | None = None,
                entity_ids: list[str] | None = None) -> pd.DataFrame:
    """Recent changes, newest first, one line per changed field."""
    query = (
        select(AuditLog, User.name)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.action.in_(("create", "update", "delete")))
        .order_by(AuditLog.changed_at.desc())
        .limit(limit)
    )
    if entity:
        query = query.where(AuditLog.entity == entity)
    if entity_ids:
        query = query.where(AuditLog.entity_id.in_(entity_ids))
    names = {"leads": "Lead", "organisations": "Institution", "contacts": "Contact"}
    rows = []
    for log, who in session.execute(query):
        diff = log.diff or {}
        what = names.get(log.entity, log.entity)
        label = diff.get("name") or diff.get("Name") or diff.get("Institution", [None, None])
        if log.action == "update":
            for field_name, change in diff.items():
                old, new = (change + [None, None])[:2] if isinstance(change, list) else (None, change)
                rows.append({"When (IST)": _ist(log.changed_at), "By": who or "", "What": what,
                             "ID": log.entity_id, "Field": field_name, "From": _fmt(old), "To": _fmt(new),
                             "Action": "Edited"})
        else:
            rows.append({"When (IST)": _ist(log.changed_at), "By": who or "", "What": what, "ID": log.entity_id,
                         "Field": "", "From": "", "To": _fmt(label if isinstance(label, str) else diff.get("institution")),
                         "Action": "Added" if log.action == "create" else "Deleted"})
    return pd.DataFrame(rows, columns=["When (IST)", "By", "Action", "What", "ID", "Field", "From", "To"])


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)
