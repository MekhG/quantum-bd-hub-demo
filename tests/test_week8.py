"""Week 8: the weekly report — outreach vs follow-ups, stages at the week's end, deals, segments, targets, notes,
final (frozen) reports, the Word and Excel downloads, and the Monday task."""

from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from docx import Document as DocxDocument
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, ActivityType, Lead, LostReason, Opportunity, Organisation, Stage, Track, User
from app.services.cadence import IST, change_stage
from app.services.quote_docx import TEMPLATE_PATH
from app.services.report_docx import report_docx, report_xlsx
from app.services.report_metrics import (
    ReportError,
    area_of,
    bucket_for,
    build_report,
    default_week,
    ensure_monday_task,
    finalise,
    get_note,
    monday_of,
    notes_for_week,
    reopen,
    report_for,
    save_comments,
    save_note,
    save_targets,
    sector_of,
    stages_at,
    targets_frame,
    targets_progress,
    touches,
    week_label,
    week_range,
)

WEEK = date(2026, 10, 12)          # a Monday
TODAY = date(2026, 10, 20)


def _user(session: Session, name: str = "Riya") -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _lead(session: Session, name: str, *, management: str | None = None, area: str | None = None,
          created: datetime | None = None) -> Lead:
    org = Organisation(name=name, org_type="university", state="Kerala", management=management, area=area)
    session.add(org)
    session.flush()
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == "cold_colleges")),
                stage=session.scalar(select(Stage).where(Stage.name == "New")), channel="Direct",
                owner_user_id=_user(session).id)
    session.add(lead)
    session.flush()
    if created is not None:
        lead.created_at = created
    session.commit()
    return lead


def _touch(session: Session, lead: Lead, type_name: str, day: date, *, hour: int = 11, owner: User | None = None,
           direction: str = "outbound") -> None:
    kind = session.scalar(select(ActivityType).where(ActivityType.name == type_name))
    session.add(Activity(type_id=kind.id, lead_id=lead.id, org_id=lead.org_id, title=f"{type_name} — test",
                         status="done", direction=direction, occurred_at=datetime.combine(day, time(hour), tzinfo=IST),
                         owner_user_id=(owner or _user(session)).id))
    session.commit()


def _move(session: Session, lead: Lead, stage: str, when: datetime) -> None:
    change_stage(session, lead, session.scalar(select(Stage).where(Stage.name == stage)), _user(session), why="test")
    session.flush()
    change = session.scalars(select(Activity).where(Activity.lead_id == lead.id, Activity.title.like("Stage:%"))
                             .order_by(Activity.id.desc())).first()
    change.occurred_at = when
    session.commit()


@pytest.fixture()
def world(seeded: Session) -> dict:
    kabir = _user(seeded, "Kabir")
    early = datetime(2026, 9, 1, tzinfo=IST)
    alpha = _lead(seeded, "Alpha University", management="State Government", area="Urban", created=early)
    beta = _lead(seeded, "Beta Institute of Technology", management="Private Un-Aided", area="Rural", created=early)
    # Alpha: first contact Monday (email + call that day = outreach), then a follow-up email and call.
    _touch(seeded, alpha, "Email", WEEK, owner=kabir)
    _touch(seeded, alpha, "Phone call", WEEK, hour=15)
    _touch(seeded, alpha, "Email", WEEK + timedelta(days=2), owner=kabir)
    _touch(seeded, alpha, "Phone call", WEEK + timedelta(days=3))
    # Beta: first contacted the week before; a WhatsApp call and a LinkedIn message this week; a reply.
    _touch(seeded, beta, "Email", WEEK - timedelta(days=5), owner=kabir)
    _touch(seeded, beta, "WhatsApp call", WEEK + timedelta(days=1))
    _touch(seeded, beta, "LinkedIn message", WEEK + timedelta(days=1), hour=16)
    _touch(seeded, beta, "Website enquiry", WEEK + timedelta(days=4), direction="inbound")
    return {"alpha": alpha, "beta": beta, "kabir": kabir}


def test_week_helpers() -> None:
    assert monday_of(date(2026, 10, 18)) == WEEK
    assert default_week(TODAY) == WEEK  # on Tuesday 20 Oct the report is about 12–18 Oct
    assert week_label(WEEK) == "12–18 Oct 2026"
    assert week_label(date(2026, 9, 28)) == "28 Sep – 4 Oct 2026"
    assert bucket_for("phone", "Phone call") == "Calls"
    assert bucket_for("whatsapp", "WhatsApp call") == "Calls"
    assert bucket_for("whatsapp", "WhatsApp message") == "WhatsApp & LinkedIn"
    assert bucket_for("email", "Email") == "Emails"
    assert bucket_for("demo", "Demo") == "Meetings & demos"
    assert sector_of("Private Un-Aided") == "Private" and sector_of("Central Government") == "Public"
    assert sector_of(None) == "Not set" and sector_of(float("nan")) == "Not set"
    assert area_of("Rural") == "Rural" and area_of(None) == "Not set"


def test_outreach_and_followups(seeded: Session, world: dict) -> None:
    start, end = week_range(WEEK)
    frame = touches(seeded, start, end)
    assert (frame["kind"] == "Outreach").sum() == 2          # Alpha's email and call on the first day
    assert frame.loc[frame["kind"] == "Outreach", "key"].nunique() == 1
    follow = frame[frame["kind"] == "Follow-up"]
    assert follow["bucket"].value_counts().to_dict() == {"Calls": 2, "Emails": 1, "WhatsApp & LinkedIn": 1}

    report = build_report(seeded, WEEK, today=TODAY)
    tiles = {t["key"]: (t["now"], t["before"]) for t in report["headline"]}
    assert tiles["outreach"] == (1, 1)                        # Beta was first contacted the week before
    assert tiles["followup:Calls"] == (2, 0)
    assert tiles["followup:Emails"] == (1, 0)
    assert tiles["replies"] == (1, 0)
    channels = {f["Channel"]: f["This week"] for f in report["followups"]}
    assert channels == {"Calls": 2, "Emails": 1, "WhatsApp & LinkedIn": 1}
    assert report["totals"]["reply_rate"] == pytest.approx(0.5)  # 1 of the 2 leads first contacted in 4 weeks
    people = {p["Person"]: p for p in report["people"]}
    assert people["Kabir Rao"]["Outreach"] == 1 and people["Kabir Rao"]["Follow-up emails"] == 1
    assert people["Riya Sharma"]["Follow-up calls"] == 2
    assert people["Riya Sharma"]["Replies"] == 1          # Beta's owner gets the reply
    segments = {s["Segment"]: s for s in report["segments"]}
    assert segments["Public · Urban"]["Contacted (week)"] == 1
    assert segments["Private · Rural"]["Replied (week)"] == 1
    assert report["trend"]["labels"][-1] == "12 Oct" and len(report["trend"]["Outreach"]) == 8


def test_stages_are_rebuilt_for_past_weeks_and_deals(seeded: Session, world: dict) -> None:
    alpha, beta = world["alpha"], world["beta"]
    in_week = datetime.combine(WEEK + timedelta(days=2), time(12), tzinfo=IST)
    after = datetime.combine(WEEK + timedelta(days=8), time(12), tzinfo=IST)
    _move(seeded, alpha, "First Contact Made", in_week)
    _move(seeded, alpha, "Online Demonstration", after)     # happened after the week: not in the week's picture
    beta.lost_reason_id = seeded.scalar(select(LostReason.id).limit(1))
    _move(seeded, beta, "Rejected", in_week)
    start, end = week_range(WEEK)
    stages = stages_at(seeded, start, end).set_index("Stage")
    assert stages.loc["First Contact Made", "Leads"] == 1
    assert stages.loc["Online Demonstration", "Leads"] == 0
    assert stages.loc["Rejected", "Leads"] == 1
    assert stages.loc["First Contact Made", "Moved in this week"] == 1

    deal = Opportunity(lead_id=alpha.id, title="Starter lab", status="won", value_inr=Decimal("1100000"),
                       po_date=WEEK + timedelta(days=4))
    seeded.add(deal)
    seeded.commit()
    report = build_report(seeded, WEEK, today=TODAY)
    assert report["won"][0]["Institution"] == "Alpha University" and report["won"][0]["Value (₹)"] == 1100000
    assert report["lost"][0]["Institution"] == "Beta Institute of Technology"
    tiles = {t["key"]: t["now"] for t in report["headline"]}
    assert tiles["won"] == 1 and report["totals"]["won_value"] == 1100000


def test_targets_copy_forward_and_progress(seeded: Session, world: dict) -> None:
    me, kabir = _user(seeded), world["kabir"]
    frame, copied = targets_frame(seeded, WEEK)
    assert not copied and frame["Outreach (first contact)"].isna().all()
    frame.loc[frame["user_id"] == kabir.id, "Outreach (first contact)"] = 2
    frame.loc[frame["user_id"] == kabir.id, "Follow-up emails"] = 4
    assert save_targets(seeded, WEEK, frame, me) == 2
    seeded.commit()
    progress = targets_progress(seeded, WEEK).set_index("Metric")
    assert progress.loc["Outreach (first contact)", "Actual"] == 1
    assert progress.loc["Outreach (first contact)", "Progress"] == pytest.approx(0.5)
    assert progress.loc["Follow-up emails", "Actual"] == 1
    # Next week starts from these targets until the admin saves new ones.
    next_frame, copied = targets_frame(seeded, WEEK + timedelta(days=7))
    assert copied and next_frame.loc[next_frame["user_id"] == kabir.id, "Follow-up emails"].iloc[0] == 4
    with pytest.raises(PermissionError):
        save_targets(seeded, WEEK, frame, kabir)
    with pytest.raises(ReportError):
        save_targets(seeded, WEEK + timedelta(days=1), frame, me)
    frame.loc[frame["user_id"] == kabir.id, "Follow-up emails"] = None   # blank removes a target
    assert save_targets(seeded, WEEK, frame, me) == 1


def test_notes_comments_finalise_and_downloads(seeded: Session, world: dict) -> None:
    me, kabir = _user(seeded), world["kabir"]
    save_note(seeded, WEEK, "Cleaned 6 bounces.", kabir)
    seeded.commit()
    assert get_note(seeded, kabir.id, WEEK) == "Cleaned 6 bounces."
    assert notes_for_week(seeded, WEEK) == [{"Person": "Kabir Rao", "Note": "Cleaned 6 bounces."}]
    with pytest.raises(PermissionError):
        save_comments(seeded, WEEK, highlights="x", risks="", asks="", user=kabir)
    save_comments(seeded, WEEK, highlights="First NQM lab lead\nTwo quotes", risks="", asks="Budget for FDPs",
                  user=me)
    finalise(seeded, WEEK, me, today=TODAY)
    seeded.commit()
    frozen, row = report_for(seeded, WEEK, today=TODAY)
    assert row.status == "final"
    # A touch logged later doesn't change a final report…
    _touch(seeded, world["alpha"], "Phone call", WEEK + timedelta(days=5))
    again, _ = report_for(seeded, WEEK, today=TODAY)
    assert {t["key"]: t["now"] for t in again["headline"]}["followup:Calls"] == 2
    with pytest.raises(ReportError):
        save_comments(seeded, WEEK, highlights="y", risks="", asks="", user=me)
    # …until it's reopened.
    reopen(seeded, WEEK, me)
    seeded.commit()
    live, _ = report_for(seeded, WEEK, today=TODAY)
    assert {t["key"]: t["now"] for t in live["headline"]}["followup:Calls"] == 3

    comments = {"highlights": row.highlights, "risks": row.risks, "asks": row.asks}
    data = report_docx(live, template=TEMPLATE_PATH.read_bytes(), comments=comments, prepared_by="Riya Sharma",
                       status="draft")
    doc = DocxDocument(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Weekly BD & Marketing Report" in text and "12–18 Oct 2026" in text
    assert "•  First NQM lab lead" in text and "Kabir Rao: Cleaned 6 bounces." in text
    assert "{greeting}" not in text                             # the quotation's text is gone…
    assert doc.sections[0].header._element.xpath(".//w:drawing")  # …the letterhead stays
    assert len(doc.inline_shapes) >= 3                            # charts
    book = pd.read_excel(io.BytesIO(report_xlsx(live, comments)), sheet_name=None)
    assert {"Summary", "Follow-ups", "Leads by stage", "Team", "Segments", "Comments"} <= set(book)
    assert book["Summary"].loc[0, "Metric"] == "Outreach"


def test_monday_task_once_a_week(seeded: Session) -> None:
    assert ensure_monday_task(seeded, TODAY) == 1
    seeded.commit()
    assert ensure_monday_task(seeded, TODAY + timedelta(days=1)) == 0
    task = seeded.scalar(select(Activity).where(Activity.title.like("Prepare last week's report%")))
    assert "12–18 Oct 2026" in task.title and task.owner_user_id == _user(seeded).id
    assert ensure_monday_task(seeded, TODAY + timedelta(days=6)) == 1   # the next Monday: a new one


def test_dashboard_additions_and_config_export(seeded: Session, world: dict) -> None:
    from app.models import Quote
    from app.services.config_queries import all_config_frames
    from app.services.dashboard_metrics import expiring_quotes, lost_reasons, won_lost_by_month

    alpha, beta = world["alpha"], world["beta"]
    deal = Opportunity(lead_id=alpha.id, title="Phase 1", status="won", value_inr=Decimal("500000"),
                       po_date=TODAY - timedelta(days=3))
    seeded.add(deal)
    seeded.flush()
    seeded.add(Quote(opportunity_id=deal.id, number="DEMO/ALU/2026-27/009", status="sent", quote_date=TODAY,
                     valid_until=TODAY + timedelta(days=3), sent_on=TODAY, subtotal_inr=Decimal("500000")))
    beta.lost_reason_id = seeded.scalar(select(LostReason.id).limit(1))
    _move(seeded, beta, "Rejected", datetime.combine(TODAY - timedelta(days=1), time(12), tzinfo=IST))
    expiring = expiring_quotes(seeded, today=TODAY)
    assert expiring["Institution"].tolist() == ["Alpha University"] and expiring["Value"].iloc[0] == 500000
    months = won_lost_by_month(seeded, today=TODAY)
    october = months[months["Month"] == "Oct 2026"].set_index("Outcome")["Count"]
    assert october["Won"] == 1 and october["Lost"] == 1
    assert lost_reasons(seeded)["Leads"].sum() == 1
    frames = all_config_frames(seeded)
    assert len(frames["Signal types"]) == 11 and len(frames["News sources"]) == 15
