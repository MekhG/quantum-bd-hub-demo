"""Week 9: events & community — Google Forms import (column guessing, consent/18+ rule, duplicates, undo), event
participation signals, creating a lead from a member, the weekly-report section."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CommunityMember, EventParticipant, Lead, Organisation, OrgSignal, Stage, Suppression, Track, User
from app.services.community import (
    CommunityError,
    EventData,
    add_participant,
    clean_url,
    community_week,
    create_lead_for_member,
    delete_member,
    events_frame,
    guess_mapping,
    level_for,
    level_rules,
    mailing_list,
    members_frame,
    parse_adult,
    parse_yes,
    participants_frame,
    preview_import,
    read_responses,
    run_import,
    save_event,
    save_participants,
    set_do_not_contact,
)
from app.services.importing.undo import undo_import

FORM = Path(__file__).parent / "fixtures" / "hackathon_form.csv"


def _user(session: Session, name: str = "Riya") -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _lead(session: Session, name: str) -> Lead:
    org = Organisation(name=name, org_type="university", state="Kerala")
    session.add(org)
    session.flush()
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == "cold_colleges")),
                stage=session.scalar(select(Stage).where(Stage.name == "New")), channel="Direct")
    session.add(lead)
    session.commit()
    return lead


def _event(session: Session, *, days_ago: int = 3, status: str = "done") -> int:
    event = save_event(session, EventData(name="Quantum Hackathon 2026", kind="Hackathon",
                                          start_on=date.today() - timedelta(days=days_ago), status=status,
                                          form_url="https://forms.gle/example"), _user(session))
    session.commit()
    return event.id


def test_guessing_columns_and_parsing() -> None:
    frame = read_responses(FORM.read_bytes(), FORM.name)
    mapping = guess_mapping(list(frame.columns))
    assert mapping["email"] == "Email Address"
    assert mapping["name"] == "Full name"
    assert mapping["team_name"] == "Team name"          # not mistaken for Name
    assert mapping["education"] == "Education / Profession"
    assert mapping["institution"] == "College / Organisation"
    assert mapping["github_url"] == "GitHub profile"
    assert mapping["linkedin_url"] == "LinkedIn profile"
    assert mapping["project_links"] == "Project links"
    assert mapping["is_adult"] == "I am 18 years or older"
    assert mapping["consent"] == "I agree to be contacted by Demo about future events"
    assert parse_yes("I agree") is True and parse_yes("No") is False and parse_yes("") is None
    assert parse_adult("17") is False and parse_adult("21 years") is True and parse_adult("Yes") is True
    assert clean_url("ashak", "github") == "https://github.com/ashak"
    assert clean_url("asha-k-quantum", "linkedin") == "https://www.linkedin.com/in/asha-k-quantum"
    assert clean_url("ashak.dev") == "https://ashak.dev"


def test_levels_follow_the_configured_words(seeded: Session) -> None:
    rules = level_rules(seeded)
    assert level_for("B.Tech ECE, 3rd year", rules) == "UG student"
    assert level_for("M.Sc Physics", rules) == "PG student"
    assert level_for("Assistant Professor, M.Tech", rules) == "Faculty"
    assert level_for("Class 12 student", rules) == "School student"
    assert level_for("B.Tech, School of ECE", rules) == "UG student"
    assert level_for("Painter", rules) == "Other"


def test_import_preview_rules_and_undo(seeded: Session) -> None:
    me = _user(seeded)
    omega = _lead(seeded, "Omega University")
    event_id = _event(seeded)
    frame = read_responses(FORM.read_bytes(), FORM.name)
    preview = preview_import(seeded, frame, guess_mapping(list(frame.columns)))
    statuses = [r.status for r in preview.rows]
    assert statuses.count("New member") == 4
    assert statuses.count("Skipped") == 2                  # no email; the same email twice
    by_email = {r.values["email"]: r for r in preview.rows if r.status != "Skipped"}
    assert by_email["ravi.m@example.edu"].values["consent"] is False   # left the consent box empty
    assert "Not mailable" in by_email["neha@example.edu"].note        # under 18
    assert by_email["asha.k@example.edu"].values["level"] == "UG student"

    batch, stats = run_import(seeded, preview, event_id=event_id, user=me, file_name=FORM.name)
    seeded.commit()
    assert stats["new members"] == 4 and stats["added to event"] == 4
    asha = seeded.scalar(select(CommunityMember).where(CommunityMember.email == "asha.k@example.edu"))
    assert asha.org_id == omega.org_id and asha.github_url == "https://github.com/ashak"
    assert asha.project_links.splitlines() == ["https://github.com/ashak/qkd-sim", "https://ashak.dev/demo"]
    ravi = seeded.scalar(select(CommunityMember).where(CommunityMember.email == "ravi.m@example.edu"))
    assert ravi.org_id is None and not ravi.consent  # 'IIT Kalpaka' isn't in the hub in this test

    members = members_frame(seeded)
    assert sorted(mailing_list(members)["Email"]) == ["asha.k@example.edu", "dr.rao@example.edu"]
    parts = participants_frame(seeded, event_id)
    assert set(parts.loc[parts["Team"] == "Qubit Crew", "Name"]) == {"Asha K", "Ravi M"}
    assert events_frame(seeded).iloc[0]["Participants"] == 4

    # The event already happened, so Omega University (a lead) gets one 'Event participation' signal.
    signals = seeded.scalars(select(OrgSignal).where(OrgSignal.org_id == omega.org_id)).all()
    assert len(signals) == 1 and seeded.get(Lead, omega.id).score == 10

    # Importing the same file again adds nobody twice.
    _, again = run_import(seeded, preview_import(seeded, frame, guess_mapping(list(frame.columns))),
                          event_id=event_id, user=me, file_name=FORM.name)
    seeded.commit()
    assert again["new members"] == 0 and again["already in event"] == 4

    undo_import(seeded, batch.id, me)
    seeded.commit()
    assert seeded.scalar(select(CommunityMember).where(CommunityMember.email == "asha.k@example.edu")) is None
    assert not seeded.scalars(select(EventParticipant)).all()


def test_institution_matching_needs_an_exact_or_short_name(seeded: Session) -> None:
    kalpaka = Organisation(name="Indian Institute of Technology Kalpaka", org_type="university")
    seeded.add(kalpaka)
    seeded.commit()
    event_id = _event(seeded, status="planned", days_ago=-10)  # a future event: no signals yet
    frame = read_responses(FORM.read_bytes(), FORM.name)
    run_import(seeded, preview_import(seeded, frame, guess_mapping(list(frame.columns))), event_id=event_id,
               user=_user(seeded), file_name=FORM.name)
    seeded.commit()
    ravi = seeded.scalar(select(CommunityMember).where(CommunityMember.email == "ravi.m@example.edu"))
    assert ravi.org_id == kalpaka.id                        # 'IIT Kalpaka' = the full name's short form
    rao = seeded.scalar(select(CommunityMember).where(CommunityMember.email == "dr.rao@example.edu"))
    assert rao.org_id is None
    assert not seeded.scalars(select(OrgSignal)).all()


def test_participants_lead_creation_and_privacy(seeded: Session) -> None:
    me = _user(seeded)
    event_id = _event(seeded)
    part = add_participant(seeded, event_id, name="Dr. S. Rao", email="dr.rao@example.edu", user=me,
                           institution="Zeta Institute of Science", role="judge", consent=True, is_adult=True)
    seeded.commit()
    with pytest.raises(CommunityError):
        add_participant(seeded, event_id, name="x", email="dr.rao@example.edu", user=me)
    frame = participants_frame(seeded, event_id)
    frame.loc[0, "Status"] = "Winner"
    frame.loc[0, "Project"] = "Quantum curriculum kit"
    assert save_participants(seeded, event_id, frame, me) == 1
    seeded.commit()
    assert seeded.get(EventParticipant, part.id).status == "winner"

    member_id = part.member_id
    track = seeded.scalar(select(Track).where(Track.code == "cold_colleges"))
    lead, note = create_lead_for_member(seeded, member_id, track.id, me)
    seeded.commit()
    assert "New organisation added: Zeta Institute of Science" in note
    assert lead.organisation.name == "Zeta Institute of Science" and lead.source == "Community / events"
    assert seeded.get(Lead, lead.id).score == 10            # the event-participation signal was backfilled
    same, note = create_lead_for_member(seeded, member_id, track.id, me)
    assert same.id == lead.id and "already a lead" in note

    set_do_not_contact(seeded, member_id, True, me)
    seeded.commit()
    assert mailing_list(members_frame(seeded)).empty
    with pytest.raises(PermissionError):
        delete_member(seeded, member_id, _user(seeded, "Kabir"))
    delete_member(seeded, member_id, me)
    seeded.commit()
    assert seeded.get(CommunityMember, member_id) is None
    assert seeded.scalars(select(Suppression)).all()
    frame = read_responses(b"Email Address,Name\ndr.rao@example.edu,Dr Rao\n", "form.csv")
    preview = preview_import(seeded, frame, guess_mapping(list(frame.columns)))
    assert preview.rows[0].status == "Skipped"              # asked to be deleted: never stored again


def test_events_validation_and_report_section(seeded: Session) -> None:
    me = _user(seeded)
    with pytest.raises(CommunityError):
        save_event(seeded, EventData(name="", kind="Hackathon", start_on=date.today()), me)
    with pytest.raises(CommunityError):
        save_event(seeded, EventData(name="X", kind="Workshop", start_on=date.today(),
                                     end_on=date.today() - timedelta(days=1)), me)
    event_id = _event(seeded, days_ago=0)
    add_participant(seeded, event_id, name="A", email="a@example.edu", user=me, institution="Omega University")
    seeded.commit()
    monday = date.today() - timedelta(days=date.today().weekday())
    week = community_week(seeded, monday, monday + timedelta(days=6))
    assert week["events"][0]["Event"] == "Quantum Hackathon 2026"
    assert week["new_members"] == 1 and week["members"] == 1 and week["mailable"] == 0
    assert week["top_institutions"][0] == {"Organisation": "Omega University", "Participants": 1}


def test_member_directory_exports_to_excel(seeded: Session) -> None:
    from app.services.lead_queries import frame_to_xlsx

    event_id = _event(seeded)
    add_participant(seeded, event_id, name="A", email="a@example.edu", user=_user(seeded))
    seeded.commit()
    frame = members_frame(seeded)
    assert frame_to_xlsx(frame.drop(columns=["id", "org_id"]), "Community")[:2] == b"PK"
