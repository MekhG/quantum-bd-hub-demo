"""Any organisation with an industry (import, tables, editing) and the speed work (cache generation, config memo,
background refresh stamp). All names are made up; emails use the reserved .example domain."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contact, Lead, Organisation, Track, User
from app.services.editing import INSTITUTIONS, LEADS, apply_plan, plan_edits
from app.services.importing.normalise import default_industry, guess_org_type, match_org_type
from app.services.importing.runner import ImportOptions, run_import
from app.services.lead_queries import INDIVIDUALS, contacts_frame, leads_frame, organisations_frame


def _admin(session: Session) -> User:
    return session.scalar(select(User).where(User.role == "admin"))


def _track(session: Session, code: str) -> int:
    return session.scalar(select(Track.id).where(Track.code == code))


def _sheet(columns: list[str], rows: list[list[str]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=columns)
    frame.index = range(2, 2 + len(frame))
    return frame


# ---------------------------------------------------------------- types and industries


@pytest.mark.parametrize(("text", "expected"), [
    ("Company", "company"), ("Pvt Ltd", "company"), ("Startup", "startup"), ("Non-profit", "non_profit"),
    ("NGO", "non_profit"), ("Government body", "government"), ("PSU", "government"),
    ("Research institute", "research_institute"), ("School", "school"), ("College", "college"),
    ("University", "university"), ("Standalone", "standalone"), ("Vidyalaxmi", None),
])
def test_type_wording_from_sheets(text: str, expected: str | None) -> None:
    assert match_org_type(text) == expected


def test_type_and_industry_guessed_from_a_name() -> None:
    assert guess_org_type("Arkavane Technologies") == "company"
    assert guess_org_type("Saptarishi STEM Foundation") == "non_profit"
    assert guess_org_type("Hemavati Vidya Mandir") == "school"
    assert guess_org_type("Swarnarekha University") == "university"
    assert guess_org_type("Something Else") == "other"
    assert default_industry("college") == "Higher education" and default_industry("company") is None


def test_industry_list_and_business_contact_types_are_seeded(seeded: Session) -> None:
    from app.models import OptionList

    industries = list(seeded.scalars(select(OptionList.value).where(OptionList.list_name == "industry")))
    types = set(seeded.scalars(select(OptionList.value).where(OptionList.list_name == "contact_type")))
    assert {"Higher education", "Telecom", "Banking & finance", "Other"} <= set(industries)
    assert {"Founder/CXO", "L&D/HR", "Engineer/Scientist", "Faculty", "HoD"} <= types
    assert seeded.scalar(select(Track).where(Track.code == "enterprise")).name == "Enterprise & public sector"


# ---------------------------------------------------------------- imports


def test_tracker_import_with_industry_and_company_types(seeded: Session) -> None:
    sheet = _sheet(
        ["Type", "Organisation Name", "Industry", "Contact Name", "Email Id", "Contact Type", "Designation"],
        [["Company", "Velmora Networks", "Telco", "Asha Rao", "asha@velmora.example", "CTO", "CTO"],
         ["", "Kestrion Capital Pvt Ltd", "BFSI", "Ravi Nair", "ravi@kestrion.example", "", "Head of L&D"],
         ["University", "Swarnarekha University", "", "Dr. Mina Das", "mina@swarnarekha.example", "HoD", "HoD"],
         ["Company", "Lumetra Labs", "Quantum magic", "Ana Roy", "ana@lumetra.example", "", "Engineer"]])
    report = run_import(seeded, sheet, ImportOptions(kind="tracker", track_id=_track(seeded, "enterprise")),
                        _admin(seeded), dry_run=False)
    seeded.flush()
    orgs = {o.name: o for o in seeded.scalars(select(Organisation))}
    assert not report.errors
    assert (orgs["Velmora Networks"].org_type, orgs["Velmora Networks"].industry) == ("company", "Telecom")
    assert (orgs["Kestrion Capital Pvt Ltd"].org_type, orgs["Kestrion Capital Pvt Ltd"].industry) == \
        ("company", "Banking & finance")  # no Type: guessed from 'Pvt Ltd'
    assert orgs["Swarnarekha University"].industry == "Higher education"  # filled in from the type
    assert orgs["Lumetra Labs"].industry is None  # unknown wording → blank, with a warning
    assert any("Industry" in issue.message for issue in report.issues if issue.level == "warning")
    contact = seeded.scalar(select(Contact).where(Contact.email == "asha@velmora.example"))
    assert contact.contact_type == "Founder/CXO"


def test_organisation_list_import_keeps_companies_apart_from_colleges(seeded: Session) -> None:
    sheet = _sheet(["Type", "Name", "Industry", "Website", "State"],
                   [["Company", "Zentrique Software Labs", "IT & software", "https://zentrique.example", "Karnataka"],
                    ["", "Aranyaka College of Science", "", "", "Kerala"],
                    ["", "Orbisol Foundation", "Non-profit & CSR", "", "Delhi"]])
    run_import(seeded, sheet, ImportOptions(kind="colleges", list_name="Mixed list"), _admin(seeded), dry_run=False)
    seeded.flush()
    kinds = {o.name: (o.org_type, o.industry) for o in seeded.scalars(select(Organisation))}
    assert kinds["Zentrique Software Labs"] == ("company", "IT & software")
    assert kinds["Aranyaka College of Science"] == ("college", "Higher education")
    assert kinds["Orbisol Foundation"] == ("non_profit", "Non-profit & CSR")


# ---------------------------------------------------------------- tables and editing


def _world(session: Session) -> Lead:
    from app.models import Stage

    company = Organisation(name="Halvora Telecom", org_type="company", industry="Telecom", state="Karnataka")
    person = Organisation(name="Priya Learner", org_type="individual")
    session.add_all([company, person])
    session.flush()
    session.add_all([Contact(org_id=company.id, name="Asha Rao", email="asha@halvora.example"),
                     Contact(org_id=person.id, name="Priya Learner", email="priya@mail.example")])
    new = session.scalar(select(Stage).where(Stage.name == "New"))
    lead = Lead(org_id=company.id, track_id=_track(session, "enterprise"), stage=new)
    individual = Lead(org_id=person.id, track_id=_track(session, "individuals"), stage=new)
    session.add_all([lead, individual])
    session.commit()
    return lead


def test_frames_carry_industry_and_individuals_get_their_own_chip(seeded: Session) -> None:
    _world(seeded)
    orgs = organisations_frame(seeded)
    assert "Priya Learner" not in set(orgs["Name"])  # people aren't organisations
    row = orgs.set_index("Name").loc["Halvora Telecom"]
    assert row["Industry"] == "Telecom" and row["Type"] == "Company" and not row["Education"]
    leads = leads_frame(seeded).set_index("Organisation")
    assert leads.loc["Halvora Telecom", "Industry"] == "Telecom"
    assert leads.loc["Priya Learner", "Industry"] == INDIVIDUALS
    contacts = contacts_frame(seeded).set_index("Name")
    assert contacts.loc["Asha Rao", "Industry"] == "Telecom" and contacts.loc["Asha Rao", "Org type"] == "Company"


def test_industry_can_be_edited_in_the_organisations_and_leads_tables(seeded: Session) -> None:
    lead = _world(seeded)
    table = organisations_frame(seeded).set_index("org_id")
    edited = table.copy()
    edited.at[lead.org_id, "Industry"] = "Banking & finance"
    result = apply_plan(seeded, plan_edits(INSTITUTIONS, table, edited, "Name"), table["_version"].to_dict(),
                        _admin(seeded))
    seeded.commit()
    assert result.saved_rows == 1 and seeded.get(Organisation, lead.org_id).industry == "Banking & finance"

    leads = leads_frame(seeded).set_index("lead_id")
    edited = leads.copy()
    edited.at[lead.id, "Industry"] = "Not an industry"
    result = apply_plan(seeded, plan_edits(LEADS, leads, edited, "Organisation"), leads["_version"].to_dict(),
                        _admin(seeded))
    assert result.saved_rows == 0 and "isn't one of the Industry options" in result.errors[0]


# ---------------------------------------------------------------- speed


def test_generation_moves_only_when_a_commit_wrote_rows(seeded: Session) -> None:
    from app.db import data_generation

    before = data_generation()
    seeded.scalars(select(Organisation)).all()
    seeded.commit()
    assert data_generation() == before
    seeded.add(Organisation(name="Trivexa Power", org_type="company", industry="Energy & utilities"))
    seeded.commit()
    assert data_generation() == before + 1


def test_config_memo_is_dropped_when_steps_change(seeded: Session) -> None:
    from app.services.cadence import _steps

    track_id = _track(seeded, "enterprise")
    steps = _steps(seeded, track_id)
    assert _steps(seeded, track_id) is steps  # remembered within the session
    steps[0].name = "Intro (edited)"
    seeded.flush()
    assert _steps(seeded, track_id) is not steps  # a config change drops the memory


def test_a_database_built_by_older_code_is_never_served(tmp_path, monkeypatch) -> None:
    from app.demo import refresh

    db_file = tmp_path / "demo.db"
    db_file.write_bytes(b"")
    monkeypatch.setattr(refresh, "DEMO_DB_PATH", db_file)
    monkeypatch.setattr(refresh, "STAMP", tmp_path / "demo.built")
    refresh.STAMP.write_text("2026-10-30")  # the old stamp format: no schema fingerprint
    assert refresh.built_for() is None and not refresh.has_data()
    refresh.STAMP.write_text(f"2026-10-30|{refresh.schema_fingerprint()}")
    assert refresh.built_for() == date(2026, 10, 30) and refresh.has_data()
    refresh.STAMP.write_text("2026-10-30|000000000000")  # tables changed since
    assert refresh.built_for() is None


def test_segments_can_be_compared_by_industry(seeded: Session) -> None:
    from app.services.diagrams import segment_comparison

    _world(seeded)
    frame = segment_comparison(seeded, date(2026, 4, 1), date(2026, 10, 30), "industry")
    assert set(frame["Category"]) == {"Telecom", "Individuals"}
    assert frame.set_index("Category").loc["Telecom", "Leads"] == 1
