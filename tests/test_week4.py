"""Week 4: query desk, tasks and handoffs, email batch, bounces, calendar, reply rates."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, AuditLog, Contact, Lead, Organisation, Stage, Track, User
from app.services.cadence import IST, complete_activity, start_cadence, today_ist
from app.services.queries import (
    NewQuery,
    QueryError,
    close_query,
    create_query,
    move_lead_track,
    queries_frame,
)
from app.services.tasks import (
    bulk_complete,
    calendar_events,
    create_task,
    email_batch_frame,
    hand_off,
    ics_for,
    mark_batch_sent,
    mark_task_bounced,
    reply_rate_frame,
)


def _user(session: Session, name: str) -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _planned(session: Session, lead: Lead) -> list[Activity]:
    return list(session.scalars(select(Activity).where(Activity.lead_id == lead.id, Activity.status == "planned")
                                .order_by(Activity.id)))


def _query(session: Session, **overrides) -> tuple[Lead, list[str]]:
    data = dict(channel="Website", received_at=datetime.now(IST) - timedelta(hours=2), name="Dr. Rho Example",
                email="rho@rho-example.ac.in", phone="9000000001", new_org_name="Rho Institute of Science",
                products=["NetSim Q"], message="Do you have a network simulator for our M.Tech lab?")
    data.update(overrides)
    return create_query(session, NewQuery(**data), _user(session, "Riya"))


def test_new_query_creates_lead_and_reply_task(seeded: Session) -> None:
    lead, messages = _query(seeded)
    seeded.commit()
    assert lead.track.code == "inbound_queries" and lead.stage.name == "New"
    assert {p.name for p in lead.product_fit} == {"NetSim Q"}
    contact = seeded.scalar(select(Contact).where(Contact.email == "rho@rho-example.ac.in"))
    assert contact.verified  # they wrote to us, so the address is real
    [reply] = _planned(seeded, lead)
    assert reply.title == "Email · Reply" and reply.owner.name == "Riya Sharma"
    assert any("Reply task created" in m for m in messages)
    queries = queries_frame(seeded)
    assert queries.iloc[0]["Status"] == "Awaiting reply" and queries.iloc[0]["Channel"] == "Website"


def test_old_unanswered_query_is_overdue(seeded: Session) -> None:
    _query(seeded, received_at=datetime.now(IST) - timedelta(days=2), email="old@rho-example.ac.in")
    assert queries_frame(seeded).iloc[0]["Status"] == "Reply overdue"


def test_replying_moves_stage_and_starts_follow_ups(seeded: Session) -> None:
    lead, _ = _query(seeded)
    riya = _user(seeded, "Riya")
    [reply] = _planned(seeded, lead)
    messages = complete_activity(seeded, reply, riya, outcome="Sent brochure and pricing")
    seeded.commit()
    assert lead.stage.name == "Replied to Queries"
    [follow_up] = _planned(seeded, lead)
    assert follow_up.title == "Phone call · Call"
    assert any("Next: Call" in m for m in messages)
    row = queries_frame(seeded).iloc[0]
    assert row["Status"] == "Following up" and row["First reply after (h)"] is not None


def test_second_query_from_same_person_counts_as_reply(seeded: Session) -> None:
    lead, _ = _query(seeded)
    [reply] = _planned(seeded, lead)
    complete_activity(seeded, reply, _user(seeded, "Riya"))
    again, _ = _query(seeded, received_at=datetime.now(IST), message="Can we schedule a demo?")
    seeded.commit()
    assert again.id == lead.id and lead.cadence_status == "replied"
    assert seeded.scalar(select(func.count(Contact.id))) == 1


def test_close_and_move_query(seeded: Session) -> None:
    lead, _ = _query(seeded)
    riya = _user(seeded, "Riya")
    mini = seeded.scalar(select(Track).where(Track.code == "individuals"))
    note = move_lead_track(seeded, lead.id, mini.id, riya)
    seeded.commit()
    assert lead.track.code == "individuals" and lead.cadence_status == "stopped" and "Moved" in note
    assert queries_frame(seeded).iloc[0]["Status"] == "Awaiting reply"  # still unanswered, whatever the track
    other, _ = _query(seeded, email="spam@spam-example.com", name="Spam", new_org_name="Spam Co")
    close_query(seeded, other.id, "Spam / duplicate", riya)
    seeded.commit()
    assert other.stage.outcome == "lost" and other.lost_reason.name == "Spam / duplicate"


def test_move_refuses_duplicate_lead(seeded: Session) -> None:
    lead, _ = _query(seeded)
    fresh = seeded.scalar(select(Track).where(Track.code == "cold_colleges"))
    seeded.add(Lead(org_id=lead.org_id, track_id=fresh.id, stage=lead.stage))
    seeded.flush()
    with pytest.raises(QueryError):
        move_lead_track(seeded, lead.id, fresh.id, _user(seeded, "Riya"))


def test_tasks_and_handoffs(seeded: Session) -> None:
    riya, kabir = _user(seeded, "Riya"), _user(seeded, "Kabir")
    task = create_task(seeded, title="Finalise reseller payment terms", day=today_ist(), owner_id=riya.id,
                       user=riya)
    message = hand_off(seeded, task.id, kabir.id, "Please draft the first version", riya)
    seeded.commit()
    assert task.owner_user_id == kabir.id and "Handed off by Riya Sharma to Kabir" in task.notes
    assert message == "Handed off to Kabir Rao."
    assert seeded.scalar(select(AuditLog).where(AuditLog.entity == "activities")).diff["Owner"] == [
        "Riya Sharma", "Kabir Rao"]
    assert bulk_complete(seeded, [task.id], kabir)[0] == "1 task(s) marked done."


def _cadence_lead(session: Session, name: str = "Sigma University") -> Lead:
    org = Organisation(name=name, org_type="university", state="Goa")
    session.add(org)
    session.flush()
    session.add(Contact(org_id=org.id, name="Dr. Sigma", email="hod@sigma-example.ac.in", verified=True))
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == "cold_colleges")),
                stage=session.scalar(select(Stage).where(Stage.name == "New")))
    session.add(lead)
    session.flush()
    start_cadence(session, [lead.id], _user(session, "Riya"))
    return lead


def test_email_batch_for_kabir_and_mark_sent(seeded: Session) -> None:
    lead = _cadence_lead(seeded)
    kabir = _user(seeded, "Kabir")
    batch = email_batch_frame(seeded, kabir.id)
    assert batch["Email"].tolist() == ["hod@sigma-example.ac.in"]
    assert batch.iloc[0]["Step"] == "Intro" and batch.iloc[0]["Track"] == "Colleges — cold outreach"
    mark_batch_sent(seeded, batch["Task ID"].tolist(), kabir)
    seeded.commit()
    assert lead.stage.name == "First Contact Made"
    assert email_batch_frame(seeded, kabir.id).empty


def test_bounce_stops_cadence_without_other_contact(seeded: Session) -> None:
    lead = _cadence_lead(seeded)
    email_task = next(a for a in _planned(seeded, lead) if a.activity_type.name == "Email")
    messages = mark_task_bounced(seeded, email_task.id, _user(seeded, "Kabir"))
    seeded.commit()
    contact = seeded.scalar(select(Contact).where(Contact.org_id == lead.org_id))
    assert contact.email_status == "bounced" and not contact.verified
    assert lead.cadence_status == "stopped" and not _planned(seeded, lead)
    assert any("Cadence stopped" in m for m in messages)
    assert email_batch_frame(seeded, None).empty


def test_calendar_and_ics(seeded: Session) -> None:
    lead = _cadence_lead(seeded)
    events = calendar_events(seeded, today_ist(), today_ist())
    assert len(events) == 2 and all(e["start"].startswith(today_ist().isoformat()) for e in events)
    ids = [a.id for a in _planned(seeded, lead)]
    text = ics_for(seeded, ids).decode()
    assert text.count("BEGIN:VEVENT") == 2 and "SUMMARY:Email · Intro" in text


def test_reply_rate(seeded: Session) -> None:
    lead = _cadence_lead(seeded)
    riya = _user(seeded, "Riya")
    for task in _planned(seeded, lead):
        complete_activity(seeded, task, riya)
    from app.services.activity_log import LogEntry, log_activity

    log_activity(seeded, LogEntry(org_id=lead.org_id, lead_id=lead.id, type_name="Email", direction="inbound",
                                  when=datetime.now(IST) + timedelta(minutes=5)), riya)
    rates = reply_rate_frame(seeded).set_index("Track")
    assert rates.loc["Colleges — cold outreach", "Contacted"] == 1
    assert rates.loc["Colleges — cold outreach", "Replied"] == 1
    assert rates.loc["Colleges — cold outreach", "Reply rate"] == 1.0
