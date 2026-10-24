"""Checks the models and seed data on a throwaway in-memory database (never touches Supabase).

Run with:  pytest
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    Base,
    CadenceStep,
    Contact,
    Lead,
    Organisation,
    Stage,
    Track,
    User,
)
from app.seed.seed_config import ACTIVITY_TYPES, STAGES, TRACKS, seed_all
from app.services.config_queries import all_config_frames, frames_to_xlsx
from app.services.privacy import email_hash


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")

    # SQLite ignores foreign keys unless asked; switch them on so tests match Postgres.
    @event.listens_for(engine, "connect")
    def _enable_fks(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_seed_fills_configuration(session: Session) -> None:
    summary = seed_all(session)
    session.commit()

    assert summary["tracks"] == len(TRACKS) == 7  # incl. Inbound queries
    assert count(session, Stage) == len(STAGES)
    assert count(session, ActivityType) == len(ACTIVITY_TYPES)
    assert count(session, User) == 5  # Riya, Kabir, Neha, Arjun + a view-only leadership login
    assert session.scalar(select(Track).where(Track.code == "individuals")) is not None


def test_seed_is_safe_to_rerun(session: Session) -> None:
    seed_all(session)
    session.commit()
    second = seed_all(session)
    session.commit()
    assert all(value == 0 for value in second.values())


def test_seed_keeps_edits(session: Session) -> None:
    seed_all(session)
    stage = session.scalar(select(Stage).where(Stage.name == "Replied to Queries"))
    stage.name = "Replied"
    user = session.scalar(select(User).where(User.name == "Kabir Rao"))
    user.email = "kabir.rao@demo.example"
    session.commit()

    seed_all(session)
    session.commit()
    assert session.scalar(select(Stage).where(Stage.name == "Replied")) is not None
    assert session.scalar(select(Stage).where(Stage.name == "Replied to Queries")) is None  # not re-added
    assert count(session, Stage) == len(STAGES)
    assert session.scalar(select(User.email).where(User.name == "Kabir Rao")) == "kabir.rao@demo.example"


def test_college_cadence_steps_and_gaps(session: Session) -> None:
    seed_all(session)
    session.commit()
    track = session.scalar(select(Track).where(Track.code == "cold_colleges"))
    steps = track.cadence_steps
    assert [s.name for s in steps] == ["Intro", "Call", "Case study email", "Break-up email"]
    second = steps[1]
    assert (second.days_after_previous, second.days_after_previous + second.grace_days) == (4, 6)
    assert {t.name for t in steps[0].activity_types} == {"Email", "LinkedIn message"}


def test_vendor_last_step_is_call_or_email(session: Session) -> None:
    seed_all(session)
    session.commit()
    last = session.scalar(
        select(CadenceStep).join(Track).where(Track.code == "edtech").order_by(CadenceStep.step_no.desc())
    )
    assert last.complete_on_any is True


def _make_lead(session: Session) -> Lead:
    seed_all(session)
    org = Organisation(name="Example Institute of Technology", state="Telangana", city="Hyderabad")
    session.add(org)
    session.flush()
    track = session.scalar(select(Track).where(Track.code == "cold_colleges"))
    stage = session.scalar(select(Stage).where(Stage.name == "New"))
    lead = Lead(org_id=org.id, track_id=track.id, stage_id=stage.id)
    session.add(lead)
    session.flush()
    return lead


def test_same_org_cannot_be_added_twice_to_one_track(session: Session) -> None:
    lead = _make_lead(session)
    session.add(Lead(org_id=lead.org_id, track_id=lead.track_id, stage_id=lead.stage_id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_org_can_sit_in_two_tracks(session: Session) -> None:
    lead = _make_lead(session)
    other = session.scalar(select(Track).where(Track.code == "referrals"))
    session.add(Lead(org_id=lead.org_id, track_id=other.id, stage_id=lead.stage_id))
    session.flush()


def test_contact_email_unique_ignoring_case(session: Session) -> None:
    session.add(Contact(name="A", email="hod.physics@example.ac.in"))
    session.flush()
    session.add(Contact(name="B", email="HOD.Physics@example.ac.in"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_planned_activity_needs_a_date(session: Session) -> None:
    lead = _make_lead(session)
    call = session.scalar(select(ActivityType).where(ActivityType.name == "WhatsApp call"))
    session.add(Activity(type_id=call.id, lead_id=lead.id, title="Check on lab budget", status="planned"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_whatsapp_meeting_with_participants(session: Session) -> None:
    lead = _make_lead(session)
    meeting = session.scalar(select(ActivityType).where(ActivityType.name == "Meeting (online)"))
    contact = Contact(org_id=lead.org_id, name="Dr. Example", email="hod@example.ac.in")
    riya = session.scalar(select(User).where(User.role == "admin"))
    session.add(contact)
    session.flush()

    activity = Activity(
        type_id=meeting.id,
        lead_id=lead.id,
        org_id=lead.org_id,
        title="QSim Cluster walkthrough with HoD",
        status="done",
        occurred_at=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc),
        direction="outbound",
        owner_user_id=riya.id,
        participants=[
            ActivityParticipant(contact_id=contact.id, role="HoD"),
            ActivityParticipant(user_id=riya.id, role="presenter"),
        ],
    )
    session.add(activity)
    session.flush()
    assert len(activity.participants) == 2


def test_participant_must_be_exactly_one_person(session: Session) -> None:
    lead = _make_lead(session)
    email_type = session.scalar(select(ActivityType).where(ActivityType.name == "Email"))
    activity = Activity(
        type_id=email_type.id, lead_id=lead.id, title="Intro", status="done", direction="outbound"
    )
    session.add(activity)
    session.flush()
    session.add(ActivityParticipant(activity_id=activity.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_email_hash_ignores_case_and_spaces() -> None:
    assert email_hash(" HoD@Example.ac.in ") == email_hash("hod@example.ac.in")
    assert len(email_hash("a@b.c")) == 64


def test_config_export_has_one_sheet_per_table(session: Session) -> None:
    seed_all(session)
    session.commit()
    frames = all_config_frames(session)
    data = frames_to_xlsx(frames)
    assert data[:2] == b"PK"  # xlsx files are zip archives
    assert len(frames["Cadences"]) == 4 + 4 + 3 + 3 + 3 + 4 + 3  # one row per cadence step, track by track
