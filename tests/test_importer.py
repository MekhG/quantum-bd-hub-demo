"""Import checks with made-up institutions and people (no real contact data)."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ConsentLog,
    Contact,
    ImportBatch,
    Lead,
    Opportunity,
    OpportunityProduct,
    Organisation,
    Suppression,
    Track,
    User,
)
from app.services.importing.columns import map_columns
from app.services.importing.normalise import OptionMatcher, parse_date, parse_location, parse_phones
from app.services.importing.readers import UploadError, read_upload
from app.services.importing.runner import ImportOptions, run_import
from app.services.importing.undo import undo_import
from app.services.privacy import email_hash

TRACKER_HEADERS = [
    "Owner", "Status", "Tier", "Potential", "Location", "Type (College/ Standalone/University)",
    "Institution Name", "Contact Name", "Email Id", "Contact Number", "Contact Type", "Department",
    "Faculty Research Interests (If Faculty)", "Product Fit (If any)",
]


def tracker_frame(rows: list[list[str]], extra: dict[str, list[str]] | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=TRACKER_HEADERS)
    for column, values in (extra or {}).items():
        frame[column] = values
    frame.index = range(2, 2 + len(frame))
    return frame


def frame(columns: list[str], rows: list[list[str]]) -> pd.DataFrame:
    result = pd.DataFrame(rows, columns=columns)
    result.index = range(2, 2 + len(result))
    return result


def track_id(session: Session, code: str) -> int:
    return session.scalar(select(Track.id).where(Track.code == code))


def admin(session: Session) -> User:
    return session.scalar(select(User).where(User.role == "admin"))


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def run(session: Session, data: pd.DataFrame, kind: str, track: str | None = "cold_colleges", dry_run=False, **kw):
    options = ImportOptions(
        kind=kind,
        track_id=track_id(session, track) if track else None,
        lead_source=kw.pop("lead_source", "Fresh"),
        file_name="test.xlsx",
        **kw,
    )
    report = run_import(session, data, options, admin(session), dry_run=dry_run)
    if not dry_run:
        session.commit()
    return report


SAMPLE_TRACKER = [
    ["Kabir", "First Contact Made", "Tier 2", "30-50%", "Warangal, Telangana", "College",
     "Example Institute of Technology", "Dr. A Rao", "a.rao@eit-example.ac.in", "98480 12345", "HoD",
     "Dept. of Physics", "Quantum optics", "QSim Cluster, QKD kit"],
    ["Kabir", "Sent Brochure", "Tier 2", "50-70%", "Warangal, Telangana", "College",
     "Example Institute of Technology", "Dr. B Iyer", "b.iyer@eit-example.ac.in", "", "Assistant Professor",
     "Computer Science and Engineering", "Quantum algorithms", "NetSim Q"],
    ["Kabir", "", "Tier 2", "", "Warangal, Telangana", "College",
     "Example Institute of Technology", "Dr. C Das", "c.das@eit-example.ac.in", "", "Faculty",
     "CSE", "", ""],
]


def test_tracker_groups_contacts_under_one_institution(seeded: Session) -> None:
    report = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    assert not report.errors
    assert count(seeded, Organisation) == 1
    assert count(seeded, Contact) == 3  # two from the same department are separate people
    assert count(seeded, ConsentLog) == 3
    lead = seeded.scalar(select(Lead))
    assert lead.stage.name == "Sent Brochure"  # most advanced status across the rows
    assert lead.potential == "50-70%"
    assert {p.name for p in lead.product_fit} == {"QSim Cluster", "Entangled-Photon QKD Kit", "NetSim Q"}
    assert lead.owner.name == "Kabir Rao"
    org = lead.organisation
    assert (org.city, org.state, org.tier, org.org_type) == ("Warangal", "Telangana", "Tier 2", "college")
    assert org.domain == "eit-example.ac.in"
    depts = set(seeded.scalars(select(Contact.department)))
    assert depts == {"Physics", "CSE"}
    rao = seeded.scalar(select(Contact).where(Contact.email == "a.rao@eit-example.ac.in"))
    assert rao.phone == "+919848012345"
    assert rao.contact_type == "HoD"
    assert report.batch_id is not None


def test_preview_saves_nothing(seeded: Session) -> None:
    report = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker", dry_run=True)
    assert report.counts["Contacts created"] == 3
    assert count(seeded, Contact) == 0
    assert count(seeded, ImportBatch) == 0


def test_reimporting_the_same_tracker_creates_no_duplicates(seeded: Session) -> None:
    run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    second = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    assert count(seeded, Organisation) == 1
    assert count(seeded, Contact) == 3
    assert count(seeded, Lead) == 1
    assert second.counts["Institutions matched"] == 1
    assert second.counts["Contacts matched"] == 3


def test_same_institution_can_go_into_a_second_track(seeded: Session) -> None:
    run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker", track="referrals", lead_source="Referral")
    assert count(seeded, Organisation) == 1
    assert count(seeded, Lead) == 2


def test_unknown_owner_becomes_lead_source(seeded: Session) -> None:
    rows = [r[:] for r in SAMPLE_TRACKER[:1]]
    rows[0][0] = "Hemant"
    report = run(seeded, tracker_frame(rows), "tracker")
    lead = seeded.scalar(select(Lead))
    assert lead.owner is None and lead.source == "Hemant"
    assert any("Hemant" in w.message for w in report.warnings)


def test_ghosted_does_not_undo_progress(seeded: Session) -> None:
    run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    rows = [r[:] for r in SAMPLE_TRACKER[:1]]
    rows[0][1] = "Ghosted"
    run(seeded, tracker_frame(rows), "tracker")
    assert seeded.scalar(select(Lead)).stage.name == "Sent Brochure"


def test_do_not_contact_list_blocks_the_contact(seeded: Session) -> None:
    seeded.add(Suppression(email_sha256=email_hash("a.rao@eit-example.ac.in"), reason="unsubscribed"))
    seeded.commit()
    report = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    emails = set(seeded.scalars(select(Contact.email)))
    assert "a.rao@eit-example.ac.in" not in emails and len(emails) == 2
    assert report.counts["Contacts skipped (do-not-contact)"] == 1


def test_colleges_sharing_a_university_domain_stay_separate(seeded: Session) -> None:
    rows = [
        ["", "", "", "", "Hyderabad, Telangana", "College", "Alpha College of Engineering", "P Q",
         "pq@stateuni.ac.in", "", "Faculty", "Physics", "", ""],
        ["", "", "", "", "Hyderabad, Telangana", "College", "Beta College of Pharmacy", "R S",
         "rs@stateuni.ac.in", "", "Faculty", "Chemistry", "", ""],
    ]
    run(seeded, tracker_frame(rows), "tracker")
    assert count(seeded, Organisation) == 2


RESPONSE_HEADERS = ["Institution Name", "Contact Name", "Email Id", "Phone Number", "Contact Type",
                    "Responses", "Owner", "Date", "Source"]


def test_responses_reply_and_enquiry(seeded: Session) -> None:
    run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    data = frame(RESPONSE_HEADERS, [
        ["Example Institute of Technology", "Dr. A Rao", "a.rao@eit-example.ac.in", "", "HoD",
         "Interested, please share pricing", "Arjun", "18/10/2026", "Email outreach"],
        ["Example Institute of Technology", "Dr. A Rao", "a.rao@eit-example.ac.in", "", "HoD",
         "Can we schedule a demo next week?", "Arjun", "19/10/2026", "Email outreach"],
        ["Gamma University", "Dr. E F", "ef@gamma-example.edu.in", "9000000001", "Dean",
         "Do you offer a QKD kit?", "Kabir", "2026-10-17", "Enquiries from website"],
        ["Gamma University", "Dr. E F", "", "", "", "No date here", "", "", "Other"],
    ])
    report = run(seeded, data, "responses")
    assert report.counts["Responses logged"] == 3
    assert [e.row for e in report.errors] == [5]  # the row without a date
    assert count(seeded, Activity) == 3
    rao_activities = seeded.scalars(
        select(Activity).join(Activity.organisation).where(Organisation.name == "Example Institute of Technology")
    ).all()
    assert len(rao_activities) == 2 and all(a.direction == "inbound" for a in rao_activities)
    gamma_lead = seeded.scalar(select(Lead).join(Lead.organisation).where(Organisation.name == "Gamma University"))
    assert gamma_lead.source == "Inbound – Website enquiry"
    assert gamma_lead.owner.name == "Kabir Rao"

    again = run(seeded, data, "responses")
    assert again.counts["Entries skipped (already logged)"] == 3
    assert count(seeded, Activity) == 3


def test_reply_from_unknown_institution_is_flagged(seeded: Session) -> None:
    data = frame(RESPONSE_HEADERS, [
        ["Nowhere College", "X", "x@nowhere-example.ac.in", "", "", "Hello", "", "18/10/2026", "Email outreach"],
    ])
    report = run(seeded, data, "responses")
    assert report.errors and "not found" in report.errors[0].message
    assert count(seeded, Organisation) == 0


CLIENT_HEADERS = ["Institution Name", "Contact Name", "Email Id", "Contact Type", "Products", "Date"]


def test_clients_schedule_deliveries(seeded: Session) -> None:
    recent = (date.today() - timedelta(days=5)).strftime("%d/%m/%Y")
    data = frame(CLIENT_HEADERS, [
        ["Delta University", "Dr. G H", "gh@delta-example.ac.in", "Director", "QSim Cluster, Quantum Sensing Kit", recent],
        ["Delta University", "Mr. I J", "ij@delta-example.ac.in", "Purchase", "QSim Cluster", recent],
        ["Epsilon College", "Dr. K L", "kl@eps-example.ac.in", "HoD", "NetSim Mini", "15/01/2025"],
    ])
    report = run(seeded, data, "clients")
    assert not report.errors
    assert count(seeded, Opportunity) == 2  # same institution + same PO date = one deal
    items = {(i.product.name, i.expected_delivery_on) for i in seeded.scalars(select(OpportunityProduct))}
    po = date.today() - timedelta(days=5)
    assert ("QSim Cluster", po + timedelta(days=21)) in items
    assert ("Quantum Sensing Kit", po + timedelta(days=70)) in items
    assert report.counts["Delivery follow-ups scheduled"] == 2
    assert report.counts["Deliveries already past due date (not scheduled)"] == 1
    stages = set(seeded.scalars(select(Lead.stage_id)))
    assert len(stages) == 1
    assert seeded.scalar(select(Lead)).stage.name == "Deal PO Released"


def test_clients_without_po_details_are_still_recorded(seeded: Session) -> None:
    data = frame(CLIENT_HEADERS, [
        ["Delta University", "", "", "", "", ""],          # client, no details yet
        ["Omega College", "", "", "", "All 5", ""],        # products but no PO date
    ])
    report = run(seeded, data, "clients")
    assert not report.errors
    stages = {l.organisation.name: l.stage.name for l in seeded.scalars(select(Lead))}
    assert stages == {"Delta University": "Deal PO Released", "Omega College": "Deal PO Released"}
    deal = seeded.scalar(select(Opportunity))
    assert deal.po_date is None and len(deal.line_items) == 5  # 'All 5' = the five main products
    assert all(i.expected_delivery_on is None for i in deal.line_items)
    assert report.counts["Clients recorded without PO details"] == 1


COLLEGE_HEADERS = ["Type", "Aishe Code", "Name", "Website", "State", "Location", "University Name"]


def test_raw_college_list_and_research_round_trip(seeded: Session) -> None:
    data = frame(COLLEGE_HEADERS, [
        ["College", "C-11111", "Zeta College of Engineering", "https://www.zeta-example.ac.in/about",
         "Telangana", "Hyderabad", "Osmania University"],
        ["Standalone", "S-22222", "Eta Institute", "", "Karnataka", "Bengaluru", ""],
        ["College", "C-11111", "Zeta College of Engineering", "", "Telangana", "Hyderabad", ""],
    ])
    preview = run(seeded, data, "colleges", track=None, dry_run=True)
    assert preview.counts["Institutions created"] == 2 and not preview.errors
    assert preview.counts["Repeated rows in this upload (skipped)"] == 1
    report = run(seeded, data, "colleges", track=None)
    assert report.counts["Institutions created"] == 2
    zeta = seeded.scalar(select(Organisation).where(Organisation.aishe_code == "C-11111"))
    assert zeta.domain == "zeta-example.ac.in" and zeta.affiliated_university == "Osmania University"
    assert count(seeded, Lead) == 0  # the raw list is a prospect pool, not leads

    # Research batch comes back with contacts and the AISHE code → joins the same institution.
    research = tracker_frame(
        [["Riya", "First Contact Made", "Tier 1", "", "Hyderabad, Telangana", "College",
          "Zeta College of Engg.", "Dr. M N", "mn@gmail.com", "", "HoD", "ECE", "", "QSim Cluster"]],
        extra={"AISHE Code": ["C-11111"]},
    )
    run(seeded, research, "tracker")
    assert count(seeded, Organisation) == 2
    assert seeded.scalar(select(Lead)).organisation.aishe_code == "C-11111"


def test_undo_removes_what_the_import_created(seeded: Session) -> None:
    report = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    result = undo_import(seeded, report.batch_id, admin(seeded))
    seeded.commit()
    assert result.deleted == {"leads": 1, "contacts": 3, "organisations": 1}
    assert count(seeded, Organisation) == count(seeded, Contact) == count(seeded, Lead) == 0
    with pytest.raises(ValueError):
        undo_import(seeded, report.batch_id, admin(seeded))


def test_undo_keeps_records_other_work_depends_on(seeded: Session) -> None:
    tracker = run(seeded, tracker_frame(SAMPLE_TRACKER), "tracker")
    run(seeded, frame(RESPONSE_HEADERS, [
        ["Example Institute of Technology", "Dr. A Rao", "a.rao@eit-example.ac.in", "", "", "Yes please",
         "", "18/10/2026", "Email outreach"],
    ]), "responses")
    result = undo_import(seeded, tracker.batch_id, admin(seeded))
    seeded.commit()
    assert result.kept["contacts"] == 1 and result.kept["leads"] == 1
    assert count(seeded, Activity) == 1


def test_missing_required_column_is_reported(seeded: Session) -> None:
    data = frame(["Contact Name", "Email Id"], [["A", "a@b-example.ac.in"]])
    report = run(seeded, data, "tracker")
    assert report.errors and "Institution Name" in report.errors[0].message


# ---------- smaller pieces ----------


def test_header_matching_ignores_brackets_and_case() -> None:
    mapping = map_columns(TRACKER_HEADERS, "tracker")
    assert mapping.ok
    assert mapping.columns["org_type"] == "Type (College/ Standalone/University)"
    assert mapping.columns["research"] == "Faculty Research Interests (If Faculty)"


def test_department_and_potential_matching() -> None:
    dept = OptionMatcher([("Physics", ["applied physics"]), ("ECE", ["electronics and communication engineering"]),
                          ("CSE", ["computer science"])])
    assert dept.match("Dept. of Physics") == "Physics"
    assert dept.match("Electronics & Communication Engineering") == "ECE"
    assert dept.match("Department of Computer Science") == "CSE"
    assert dept.match("Mechanical") is None
    potential = OptionMatcher([("30-50%", None), ("100%", None)], compact=True)
    assert potential.match("30 - 50 %") == "30-50%"
    assert potential.match("30 to 50%") == "30-50%"


def test_small_parsers() -> None:
    assert parse_date("18/10/2026") == date(2026, 10, 18)
    assert parse_date("2026-10-18 00:00:00") == date(2026, 10, 18)
    assert parse_date("03/04/2026") == date(2026, 4, 3)  # day first, Indian style
    assert parse_date("46313") == date(2026, 10, 18)
    assert parse_location("Pondicherry") == (None, "Puducherry")
    assert parse_location("Pune, Maharashtra") == ("Pune", "Maharashtra")
    assert parse_phones("98480 12345 / 040-2345678") == ("+919848012345", ["0402345678"])


def test_reader_finds_header_below_a_title_row() -> None:
    raw = pd.DataFrame([["Lead tracker — Oct", "", ""], TRACKER_HEADERS[:3], ["Kabir", "Sent Brochure", "Tier 1"]])
    buffer = BytesIO()
    raw.to_excel(buffer, header=False, index=False)
    sheets = read_upload(buffer.getvalue(), "tracker.xlsx")
    assert list(sheets[0].frame.columns) == TRACKER_HEADERS[:3]
    assert sheets[0].frame.index[0] == 3  # spreadsheet row number for messages


def test_reader_rejects_other_files() -> None:
    with pytest.raises(UploadError):
        read_upload(b"hello", "notes.txt")
    with pytest.raises(UploadError):
        read_upload(b"not a zip", "fake.xlsx")


# ---------- slice 2.1: shapes found in the team's real sheets ----------


def test_raw_list_types_from_aishe_prefix_areas_management_and_lists(seeded: Session) -> None:
    data = frame(
        ["Type", "Aishe Code", "Name", "Website", "State", "Location", "University Name", "University Type/Management"],
        [
            ["Standalone", "S-87", "Kappa Institute of Management", "https://kappa-example.ac.in", "Goa", "Rural", "",
             "Private Un-Aided"],
            ["Vidyalaxmi", "U-0121", "Kappa University", "", "Goa", "", "", "State Government"],
            ["Colleges", "C-5", "Lambda College", "", "Goa", "Urban", "Kappa University", "State Public University"],
        ],
    )
    run(seeded, data, "colleges", track=None, list_name="AISHE filtered")
    orgs = {o.aishe_code: o for o in seeded.scalars(select(Organisation))}
    assert orgs["U-0121"].org_type == "university"  # 'Vidyalaxmi' is a list, not a type
    assert orgs["U-0121"].source_lists == "AISHE filtered, Vidyalaxmi"
    assert orgs["S-87"].area == "Rural" and orgs["S-87"].city is None
    assert orgs["S-87"].management == "Private Un-Aided"
    # For colleges this column is the affiliating university's type, not the college's management.
    assert orgs["C-5"].management is None and orgs["C-5"].org_type == "college"


def test_full_aishe_extract_columns(seeded: Session) -> None:
    data = frame(
        ["Aishe Code", "Name", "State", "District", "Website", "Year Of Establishment", "Location", "College Type",
         "Manegement", "University Aishe Code", "University Name", "University Type"],
        [["C-60180", "Mu College", "Andaman and Nicobar Islands", "South Andamans", "www.mu-example.org", "2015",
          "Urban", "Affiliated College", "Central Government", "U-0369", "Pondicherry University",
          "Central University"]],
    )
    run(seeded, data, "colleges", track=None)
    org = seeded.scalar(select(Organisation))
    assert (org.city, org.area, org.category, org.management, org.affiliated_university) == (
        "South Andamans", "Urban", "Affiliated College", "Central Government", "Pondicherry University")


def test_tracker_designation_interest_areas_and_summary_notes(seeded: Session) -> None:
    rows = [r[:] for r in SAMPLE_TRACKER]
    rows[0][10] = "HOD / Professor"
    rows[1][10] = "Executive Lead"
    rows[0][13] = "Quantum Computing Curriculum; Qsim; NetSim Q; Faculty Training/Enablement"
    report = run(seeded, tracker_frame(rows, extra={"Product Fit 2": ["Quantum Awareness", "", ""]}), "tracker")
    rao = seeded.scalar(select(Contact).where(Contact.email == "a.rao@eit-example.ac.in"))
    assert (rao.contact_type, rao.designation) == ("HoD", "HOD / Professor")
    iyer = seeded.scalar(select(Contact).where(Contact.email == "b.iyer@eit-example.ac.in"))
    assert iyer.contact_type is None and iyer.designation == "Executive Lead"
    lead = seeded.scalar(select(Lead))
    assert {p.name for p in lead.product_fit} >= {"QSim Cluster", "NetSim Q"}
    assert lead.interest_areas == "Quantum Computing Curriculum; Faculty Training/Enablement; Quantum Awareness"
    messages = [w.message for w in report.warnings]
    assert any(m.startswith("Contact type: 1 rows") for m in messages)
    assert any("Interest areas" in m for m in messages)


def test_fill_down_and_state_headings(seeded: Session) -> None:
    data = frame(["S.NO", "UNIVERSITY", "PROFESSOR/HOD", "EMAIL", "REMARKS (IF ANY)"], [
        ["", "ANDHRA PRADESH", "", "", ""],
        ["1", "Nu Technological University", "Dr. P", "p@nu-example.ac.in", ""],
        ["", "", "Dr. Q", "q@nu-example.ac.in", "Physics HoD"],
        ["", "TELANGANA", "", "", ""],
        ["2", "Xi University", "Dr. R", "r@xi-example.ac.in", ""],
    ])
    report = run(seeded, data, "tracker", track="referrals", fill_down=True)
    assert not report.errors
    assert report.counts["State heading rows (used, not imported)"] == 2
    orgs = {o.name: o.state for o in seeded.scalars(select(Organisation))}
    assert orgs == {"Nu Technological University": "Andhra Pradesh", "Xi University": "Telangana"}
    assert count(seeded, Contact) == 3


def test_kabir_style_headers(seeded: Session) -> None:
    data = frame(["S.No", "University", "HOD/Prof", "Email", "Contacts", "Products Enquired for",
                  "Funding Source/Grant", "Remarks"], [
        ["1", "Pi Rural Institute", "Dr. S", "s@pi-example.ac.in", "9000000002", "QKD, NetSim Q, QM, QS", "UG Lab",
         "Awaiting Funding"],
    ])
    report = run(seeded, data, "tracker", track="referrals")
    assert not report.errors
    lead = seeded.scalar(select(Lead))
    assert {p.name for p in lead.product_fit} == {
        "Entangled-Photon QKD Kit", "NetSim Q", "Quantum Memory Kit",
        "Quantum Sensing Kit"}
    assert "Funding: UG Lab" in lead.notes and "Awaiting Funding" in lead.notes
    assert lead.nqm_flag is True  # 'UG Lab' funding = NQM applicant
    assert report.counts["Leads marked NQM (from Funding)"] == 1
    assert seeded.scalar(select(Contact)).phone == "+919000000002"


def test_other_funding_does_not_mark_nqm(seeded: Session) -> None:
    data = frame(["University", "Email", "Funding Source/Grant"], [
        ["Rho University", "a@rho-example.ac.in", "DST FIST"],
        ["Sigma University", "b@sigma-example.ac.in", "Drug lab grant"],
        ["Tau University", "c@tau-example.ac.in", "NQM - UG Lab (applied)"],
    ])
    report = run(seeded, data, "tracker", track="referrals")
    assert not report.errors
    flags = {lead.organisation.name: lead.nqm_flag for lead in seeded.scalars(select(Lead))}
    assert flags == {"Rho University": False, "Sigma University": False, "Tau University": True}


def test_channel_column(seeded: Session) -> None:
    run(seeded, tracker_frame(SAMPLE_TRACKER[:1], extra={"Channel": ["Reseller lead"]}), "tracker")
    assert seeded.scalar(select(Lead)).channel == "Reseller"


MINI_HEADERS = ["Date", "Name", "Email", "Contact number", "Product shown interest in", "Status", "Potential",
                "About", "Note"]


def test_mini_products_individuals(seeded: Session) -> None:
    data = frame(MINI_HEADERS, [
        ["10/10/2026", "Rho Student", "rho@gmail.com", "9000000003", "QSim Mini", "Online Demonstration",
         "50-70%", "MSc Physics student", "Wants a demo for her lab group"],
        ["12/10/2026", "Rho Student", "rho@gmail.com", "", "NetSim Mini", "", "", "", "Asked for pricing"],
        ["", "Sigma Person", "sigma@outlook.com", "", "NetSim Mini", "", "", "", ""],
    ])
    report = run(seeded, data, "mini", track=None, lead_source="Mini products")
    assert not report.errors
    leads = seeded.scalars(select(Lead)).all()
    assert len(leads) == 2 and all(l.track.code == "individuals" for l in leads)
    rho = next(l for l in leads if l.organisation.name == "Rho Student")
    assert rho.organisation.org_type == "individual"
    assert {p.name for p in rho.product_fit} == {"QSim Mini", "NetSim Mini"}
    assert rho.stage.name == "Online Demonstration" and rho.potential == "50-70%"
    assert count(seeded, Activity) == 2  # two dated entries for Rho, none for the undated row
    assert "About: MSc Physics student" in seeded.scalar(select(Contact).where(Contact.email == "rho@gmail.com")).notes
    again = run(seeded, data, "mini", track=None, lead_source="Mini products")
    assert count(seeded, Lead) == 2 and count(seeded, Activity) == 2
    assert again.counts["Entries skipped (already logged)"] == 2


def test_same_name_colleges_with_different_aishe_codes_stay_separate(seeded: Session) -> None:
    data = frame(COLLEGE_HEADERS, [
        ["College", "C-1", "Government Degree College", "", "Jammu and Kashmir", "Rural", ""],
        ["College", "C-2", "Government Degree College", "", "Jammu and Kashmir", "Urban", ""],
        ["College", "C-3", "Government Degree College", "", "Jammu and Kashmir", "Rural", ""],
    ])
    report = run(seeded, data, "colleges", track=None)
    assert not report.errors and count(seeded, Organisation) == 3
    again = run(seeded, data, "colleges", track=None)
    assert again.counts["Institutions matched"] == 3 and count(seeded, Organisation) == 3


def test_bare_name_column_is_the_contact(seeded: Session) -> None:
    data = frame(["Institution Name", "Name", "Email"], [["Upsilon University", "Dr. U Example", "u@ups-example.ac.in"]])
    report = run(seeded, data, "tracker")
    assert not report.errors
    assert seeded.scalar(select(Contact)).name == "Dr. U Example"
