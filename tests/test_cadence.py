"""Cadence engine and activity logging."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, AuditLog, Contact, Lead, Organisation, Stage, Track, User
from app.services.activity_log import LogEntry, NextStep, ghost_frame, log_activity, task_frame
from app.services.cadence import (
    IST,
    complete_activity,
    mark_ghosted,
    run_daily_checks,
    skip_activity,
    start_cadence,
    today_ist,
)
from app.services.editing import LEADS, apply_plan, plan_edits
from app.services.importing.runner import ImportOptions, run_import
from app.services.lead_queries import leads_frame


def _user(session: Session, name: str) -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _lead(session: Session, name: str = "Kappa University", *, verified: bool = True, track: str = "cold_colleges") -> Lead:
    org = Organisation(name=name, org_type="university", state="Kerala")
    session.add(org)
    session.flush()
    session.add(Contact(org_id=org.id, name=f"Dr. {name[:5]}", email=f"hod@{name.split()[0].lower()}-example.ac.in",
                        verified=verified))
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == track)),
                stage=session.scalar(select(Stage).where(Stage.name == "New")))
    session.add(lead)
    session.commit()
    return lead


def _planned(session: Session, lead: Lead) -> list[Activity]:
    return list(session.scalars(select(Activity).where(Activity.lead_id == lead.id, Activity.status == "planned")
                                .order_by(Activity.id)))


def test_start_plans_step_one_for_the_right_people(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    result = start_cadence(seeded, [lead.id], riya)
    seeded.commit()
    assert [lead_id for lead_id, _ in result.started] == [lead.id]
    planned = _planned(seeded, lead)
    owners = {a.activity_type.name: a.owner.name for a in planned}
    assert owners == {"Email": "Kabir Rao", "LinkedIn message": "Riya Sharma"}  # default task assignments
    assert all(a.participants and a.participants[0].contact.email.startswith("hod@") for a in planned)
    assert lead.cadence_status == "active" and lead.current_step_no == 1 and lead.cadence_started_on == today_ist()
    assert seeded.scalar(select(AuditLog).where(AuditLog.entity == "leads")).diff["Cadence"][1].startswith("Started")


def test_unverified_or_closed_leads_are_skipped(seeded: Session) -> None:
    lead = _lead(seeded, verified=False)
    result = start_cadence(seeded, [lead.id], _user(seeded, "Riya"))
    assert not result.started and "no verified contact" in result.skipped[0][1]


def test_steps_follow_each_other_then_ghosted(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya)
    today = today_ist()
    email, linkedin = _planned(seeded, lead)

    complete_activity(seeded, email, riya, outcome="Sent")
    assert lead.stage.name == "First Contact Made" and lead.stage_changed_at is not None
    assert lead.current_step_no == 1  # the LinkedIn message is still open
    messages = skip_activity(seeded, linkedin, riya, "not on LinkedIn")
    assert lead.current_step_no == 2 and lead.cadence_due_on == today + timedelta(days=4)
    assert "Call" in messages[0]

    for _ in range(3):  # steps 2–4
        for activity in _planned(seeded, lead):
            complete_activity(seeded, activity, riya)
    assert lead.cadence_status == "awaiting_reply" and not _planned(seeded, lead)
    assert lead.cadence_due_on == today + timedelta(days=10)  # last step's gap (7) + grace (3)

    run_daily_checks(seeded, today=today + timedelta(days=9))
    assert lead.cadence_status == "awaiting_reply"
    run_daily_checks(seeded, today=today + timedelta(days=10))
    assert lead.cadence_status == "finished"
    assert ghost_frame(seeded, owner_id=None)["lead_id"].tolist() == [lead.id]

    mark_ghosted(seeded, lead, riya)
    seeded.commit()
    assert lead.stage.name == "Ghosted" and lead.cadence_status == "ghosted" and lead.cadence_ended_on == today
    assert ghost_frame(seeded, owner_id=None).empty


def test_reply_stops_cadence_and_moves_stage(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya)
    contact_id = seeded.scalar(select(Contact.id).where(Contact.org_id == lead.org_id))
    _, messages = log_activity(seeded, LogEntry(org_id=lead.org_id, lead_id=lead.id, type_name="Email",
                                                direction="inbound", when=datetime.now(IST),
                                                notes="Please send the brochure", contact_ids=[contact_id]),
                               riya)
    seeded.commit()
    assert lead.cadence_status == "replied" and not _planned(seeded, lead)
    assert lead.stage.name == "Replied to Queries"
    assert any("they replied" in m for m in messages)


def test_logging_a_planned_task_ticks_it_off(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya)
    before = seeded.scalar(select(func.count(Activity.id)))
    activity, messages = log_activity(
        seeded,
        LogEntry(org_id=lead.org_id, lead_id=lead.id, type_name="Email", when=datetime.now(IST),
                 outcome="Sent the intro", next_step=NextStep("Demo", today_ist() + timedelta(days=2))),
        riya,
    )
    seeded.commit()
    assert activity.cadence_step_id is not None and activity.status == "done"
    assert "Ticked off" in messages[0]
    assert seeded.scalar(select(func.count(Activity.id))) == before + 2  # stage change + the planned demo
    demo = seeded.scalar(select(Activity).where(Activity.follow_up_of_id == activity.id))
    assert demo.status == "planned" and demo.activity_type.name == "Demo"


def test_daily_cap_spreads_manual_starts_and_limits_auto_starts(seeded: Session) -> None:
    track = seeded.scalar(select(Track).where(Track.code == "cold_colleges"))
    track.daily_start_cap = 1
    leads = [_lead(seeded, f"Lambda{i} University") for i in range(3)]
    result = start_cadence(seeded, [lead.id for lead in leads], _user(seeded, "Riya"))
    today = today_ist()
    assert list(result.by_day().values()) == [1, 1, 1]
    assert list(result.by_day()) == [today, today + timedelta(days=1), today + timedelta(days=2)]

    track.auto_start_cadence = True
    extra = [_lead(seeded, f"Mu{i} University") for i in range(2)]
    run_daily_checks(seeded, today=today + timedelta(days=3))
    states = [lead.cadence_status for lead in extra]
    assert states.count("active") == 1 and states.count(None) == 1


def test_auto_start_needs_a_verified_contact(seeded: Session) -> None:
    seeded.scalar(select(Track).where(Track.code == "cold_colleges")).auto_start_cadence = True
    ready, waiting = _lead(seeded, "Nu University"), _lead(seeded, "Xi University", verified=False)
    check = run_daily_checks(seeded)
    assert [lead_id for lead_id, _ in check.auto_started.started] == [ready.id]
    assert waiting.cadence_status is None


def test_moving_the_stage_by_hand_stops_the_cadence(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya)
    seeded.commit()
    original = leads_frame(seeded).set_index("lead_id")
    edited = original.copy()
    edited.at[lead.id, "Stage"] = "Sent Brochure"
    apply_plan(seeded, plan_edits(LEADS, original, edited, "Institution"), original["_version"].to_dict(), riya)
    seeded.commit()
    seeded.refresh(lead)
    assert lead.cadence_status == "stopped" and not _planned(seeded, lead)


def test_today_buckets(seeded: Session) -> None:
    lead = _lead(seeded)
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya, on=today_ist() - timedelta(days=2))
    frame = task_frame(seeded, owner_id=None)
    assert set(frame["Bucket"]) == {"Overdue"}  # step 1 has one grace day
    mine = task_frame(seeded, owner_id=_user(seeded, "Kabir").id)
    assert mine["Type"].tolist() == ["Email"] and mine["Email"].iloc[0].startswith("hod@")


def test_response_import_counts_as_reply(seeded: Session) -> None:
    lead = _lead(seeded, "Omicron University")
    riya = _user(seeded, "Riya")
    start_cadence(seeded, [lead.id], riya)
    seeded.commit()
    data = pd.DataFrame([["Omicron University", "Dr. Omicr", "hod@omicron-example.ac.in", "Interested",
                          "2026-10-18", "Email outreach"]],
                        columns=["Institution Name", "Contact Name", "Email Id", "Responses", "Date", "Source"])
    options = ImportOptions(kind="responses", track_id=lead.track_id, file_name="r.xlsx",
                            collection_method="They contacted us (enquiry / reply)")
    report = run_import(seeded, data, options, riya, dry_run=False)
    seeded.commit()
    seeded.refresh(lead)
    assert not report.errors
    assert lead.cadence_status == "replied" and lead.stage.name == "Replied to Queries"
