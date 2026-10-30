"""Editing tables: change detection, validation, conflicts, audit log, stage rules, deletes, add contact, paste."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, AuditLog, ConsentLog, Contact, Lead, Organisation, Stage, Suppression, Track, User
from app.services.editing import (
    CONTACTS,
    INSTITUTIONS,
    LEADS,
    EditError,
    NewContact,
    add_contact,
    apply_plan,
    current_version,
    plan_edits,
)
from app.services.importing.readers import UploadError, read_pasted
from app.services.lead_queries import audit_frame, colleges_frame, contacts_frame, leads_frame
from app.services.privacy import email_hash


def _user(session: Session, role: str) -> User:
    user = session.scalar(select(User).where(User.role == role))
    if user is None:
        user = User(name=f"Test {role}", email=f"{role}@example.invalid", role=role)
        session.add(user)
        session.flush()
    return user


def _setup(session: Session) -> Lead:
    org = Organisation(name="Zeta University", org_type="university", state="Kerala")
    session.add(org)
    session.flush()
    session.add_all([
        Contact(org_id=org.id, name="Dr. Z One", email="z1@zeta-example.ac.in"),
        Contact(org_id=org.id, name="Dr. Z Two", email="z2@zeta-example.ac.in"),
    ])
    track = session.scalar(select(Track).where(Track.code == "cold_colleges"))
    lead = Lead(organisation=org, track_id=track.id, stage=session.scalar(select(Stage).where(Stage.name == "New")))
    session.add(lead)
    session.commit()
    return lead


def _grid(frame: pd.DataFrame, id_column: str) -> tuple[pd.DataFrame, dict[int, str]]:
    table = frame.set_index(id_column)
    return table, table["_version"].to_dict()


def test_new_stage_list_is_seeded(seeded: Session) -> None:
    names = list(seeded.scalars(select(Stage.name).order_by(Stage.sort_order)))
    assert names == ["New", "First Contact Made", "Replied to Queries", "Sent Brochure", "Online Demonstration",
                     "Offline Demonstration", "Sent Quotation", "Deal PO Released", "Ghosted", "Rejected"]


def test_edit_lead_and_institution_fields(seeded: Session) -> None:
    lead = _setup(seeded)
    original, versions = _grid(leads_frame(seeded), "lead_id")
    edited = original.copy()
    edited.at[lead.id, "Stage"] = "Sent Brochure"
    edited.at[lead.id, "Tier"] = "Tier 1"
    edited.at[lead.id, "Potential"] = "50-70%"
    edited.at[lead.id, "Product fit"] = ["NetSim Q", "QSim Cluster"]
    plan = plan_edits(LEADS, original, edited, "Organisation")
    assert {c for e in plan.edits for c in e.values} == {"Stage", "Tier", "Potential", "Product fit"}

    result = apply_plan(seeded, plan, versions, _user(seeded, "member"))
    seeded.commit()
    assert result.saved_rows == 1 and not result.errors and not result.conflicts
    seeded.refresh(lead)
    assert lead.stage.name == "Sent Brochure" and lead.potential == "50-70%"
    assert lead.organisation.tier == "Tier 1"
    assert {p.name for p in lead.product_fit} == {"NetSim Q", "QSim Cluster"}
    # Stage change is on the timeline; every change is in the audit log.
    activity = seeded.scalar(select(Activity).where(Activity.lead_id == lead.id))
    assert activity.title == "Stage: New → Sent Brochure" and activity.activity_type.name == "Stage change"
    logs = list(seeded.scalars(select(AuditLog).where(AuditLog.action == "update")))
    assert {log.entity for log in logs} == {"leads", "organisations"}
    history = audit_frame(seeded)
    assert set(history["Field"]) >= {"Stage", "Tier", "Potential", "Product fit"}


def test_rejected_needs_a_reason(seeded: Session) -> None:
    lead = _setup(seeded)
    original, versions = _grid(leads_frame(seeded), "lead_id")
    edited = original.copy()
    edited.at[lead.id, "Stage"] = "Rejected"
    result = apply_plan(seeded, plan_edits(LEADS, original, edited, "Organisation"), versions,
                        _user(seeded, "member"))
    assert result.saved_rows == 0 and "Rejected reason" in result.errors[0]
    seeded.rollback()

    edited.at[lead.id, "Rejected reason"] = "Budget"
    result = apply_plan(seeded, plan_edits(LEADS, original, edited, "Organisation"), versions,
                        _user(seeded, "member"))
    seeded.commit()
    seeded.refresh(lead)
    assert result.saved_rows == 1 and lead.stage.name == "Rejected" and lead.lost_reason.name == "Budget"


def test_conflicting_edit_is_skipped(seeded: Session) -> None:
    lead = _setup(seeded)
    original, versions = _grid(leads_frame(seeded), "lead_id")
    versions[lead.id] = "stale-version"  # as if someone saved this row after we loaded the table
    edited = original.copy()
    edited.at[lead.id, "Potential"] = "70-90%"
    result = apply_plan(seeded, plan_edits(LEADS, original, edited, "Organisation"), versions,
                        _user(seeded, "member"))
    assert result.saved_rows == 0 and "changed by someone else" in result.conflicts[0]


def test_versions_from_table_match_database(seeded: Session) -> None:
    lead = _setup(seeded)
    frame = leads_frame(seeded).set_index("lead_id")
    assert frame.at[lead.id, "_version"] == current_version(LEADS, lead)


def test_contact_email_validation_and_uniqueness(seeded: Session) -> None:
    _setup(seeded)
    original, versions = _grid(contacts_frame(seeded), "contact_id")
    first, second = original.index[:2]
    edited = original.copy()
    edited.at[first, "Email"] = "not-an-email"
    edited.at[second, "Email"] = original.at[first, "Email"].upper()
    result = apply_plan(seeded, plan_edits(CONTACTS, original, edited, "Name"), versions, _user(seeded, "member"))
    assert result.saved_rows == 0 and len(result.errors) == 2
    assert any("doesn't look like an email" in e for e in result.errors)
    assert any("already has the email" in e for e in result.errors)


def test_do_not_contact_adds_suppression(seeded: Session) -> None:
    _setup(seeded)
    original, versions = _grid(contacts_frame(seeded), "contact_id")
    contact_id = original.index[0]
    edited = original.copy()
    edited.at[contact_id, "Do not contact"] = True
    edited.at[contact_id, "Verified"] = True
    edited.at[contact_id, "Phone"] = "98765 43210"
    result = apply_plan(seeded, plan_edits(CONTACTS, original, edited, "Name"), versions, _user(seeded, "member"))
    seeded.commit()
    contact = seeded.get(Contact, int(contact_id))
    assert result.saved_rows == 1 and contact.do_not_contact and contact.verified and contact.verified_on
    assert contact.phone == "+919876543210"
    assert seeded.scalar(select(Suppression).where(Suppression.email_sha256 == email_hash(contact.email)))


def test_members_cannot_delete_and_admin_delete_rules(seeded: Session) -> None:
    lead = _setup(seeded)
    original, versions = _grid(colleges_frame(seeded), "org_id")
    edited = original.drop(index=[lead.org_id])
    plan = plan_edits(INSTITUTIONS, original, edited, "Name")
    assert plan.deletes == [(lead.org_id, "Zeta University")]
    with pytest.raises(PermissionError):
        apply_plan(seeded, plan, versions, _user(seeded, "member"))
    result = apply_plan(seeded, plan, versions, _user(seeded, "admin"))
    assert result.deleted == 0 and "delete those first" in result.errors[0]

    contacts, contact_versions = _grid(contacts_frame(seeded), "contact_id")
    plan = plan_edits(CONTACTS, contacts, contacts.iloc[1:], "Name")
    result = apply_plan(seeded, plan, contact_versions, _user(seeded, "admin"))
    seeded.commit()
    assert result.deleted == 1 and seeded.scalar(select(func.count(Contact.id))) == 1
    assert seeded.scalar(select(AuditLog).where(AuditLog.action == "delete")).entity == "contacts"


def test_institution_edits(seeded: Session) -> None:
    lead = _setup(seeded)
    original, versions = _grid(colleges_frame(seeded), "org_id")
    edited = original.copy()
    edited.at[lead.org_id, "Website"] = "www.zeta-example.ac.in"
    edited.at[lead.org_id, "AISHE Code"] = "u-0999"
    edited.at[lead.org_id, "State"] = "kerala"  # wording is normalised
    result = apply_plan(seeded, plan_edits(INSTITUTIONS, original, edited, "Name"), versions,
                        _user(seeded, "member"))
    seeded.commit()
    org = seeded.get(Organisation, lead.org_id)
    assert result.saved_rows == 1, result.errors
    assert org.website == "https://www.zeta-example.ac.in" and org.domain == "zeta-example.ac.in"
    assert org.aishe_code == "U-0999"


def test_add_contact_by_hand(seeded: Session) -> None:
    lead = _setup(seeded)
    member = _user(seeded, "member")
    contact = add_contact(seeded, NewContact(org_id=lead.org_id, name="Prof. New", email="New@Zeta-Example.ac.in",
                                             phone="9000000009", contact_type="HoD",
                                             collection_method="Event or meeting (shared their details)"), member)
    seeded.commit()
    assert contact.email == "new@zeta-example.ac.in" and contact.phone == "+919000000009"
    consent = seeded.scalar(select(ConsentLog).where(ConsentLog.contact_id == contact.id))
    assert consent.basis == "consent"
    with pytest.raises(EditError):
        add_contact(seeded, NewContact(org_id=lead.org_id, name="Dup", email="new@zeta-example.ac.in"), member)


def test_paste_formats() -> None:
    tsv = "Institution Name\tEmail Id\nAlpha University\ta@alpha-example.ac.in\n"
    assert read_pasted(tsv).frame.iloc[0].tolist() == ["Alpha University", "a@alpha-example.ac.in"]
    markdown = ("Sure, here's the table:\n\n| Institution Name | Email Id |\n|---|---|\n"
                "| **Beta College** | b@beta-example.ac.in |\n")
    assert read_pasted(markdown).frame.iloc[0].tolist() == ["Beta College", "b@beta-example.ac.in"]
    with pytest.raises(UploadError):
        read_pasted("Institution Name\tEmail Id\n")


def test_old_sheet_wording_maps_to_new_stages(seeded: Session) -> None:
    from app.services.importing.lookups import load_lookups

    lookups = load_lookups(seeded, include_people=False)
    assert lookups.find_stage("Physical Demonstration").name == "Offline Demonstration"
    assert lookups.find_stage("lost").name == "Rejected"
    assert lookups.find_stage("Negotiation").name == "Sent Quotation"
    assert lookups.find_stage("Verified").name == "New"
    assert lookups.first_stage.name == "New" and lookups.won_stage.name == "Deal PO Released"


def test_export_everything(seeded: Session) -> None:
    from io import BytesIO

    from app.services.backup import export_everything

    _setup(seeded)
    data, counts = export_everything(seeded, None)
    assert counts["organisations"] == 1 and counts["contacts"] == 2 and counts["stages"] == 10
    book = pd.read_excel(BytesIO(data), sheet_name=None)
    assert {"leads", "contacts", "organisations", "audit_log"} <= set(book)


def test_bulk_mark_verified_and_do_not_contact(seeded: Session) -> None:
    from app.services.editing import bulk_set_flag

    _setup(seeded)
    frame = contacts_frame(seeded).set_index("contact_id")
    member = _user(seeded, "member")
    result = bulk_set_flag(seeded, CONTACTS, frame, "Verified", True, "Name", member)
    seeded.commit()
    assert result.saved_rows == 2
    assert all(c.verified and c.verified_on for c in seeded.scalars(select(Contact)))
    # Already-verified rows are left alone; do-not-contact also fills the suppression list.
    frame = contacts_frame(seeded).set_index("contact_id")
    assert bulk_set_flag(seeded, CONTACTS, frame, "Verified", True, "Name", member).saved_rows == 0
    result = bulk_set_flag(seeded, CONTACTS, frame.head(1), "Do not contact", True, "Name", member)
    seeded.commit()
    assert result.saved_rows == 1 and seeded.scalar(select(func.count(Suppression.id))) == 1
