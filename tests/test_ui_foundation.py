"""UI foundation step: global search, alerts, weekly summary, dashboard periods, richer Today task data, and
the shared HTML building blocks."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, Contact, Lead, Organisation, Stage, Track, User
from app.services.activity_log import task_frame
from app.services.cadence import IST, complete_activity, start_cadence
from app.services.dashboard_metrics import kpis, period_bounds, team_activity, upcoming_demos, weekly_series
from app.services.notifications import shell_counts
from app.services.queries import NewQuery, create_query
from app.services.search import search_everything
from app.services.tasks import create_task, hand_off, week_summary
from app.ui.components import chip, chips, esc, initials, stage_stepper


def _user(session: Session, name: str) -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _lead(session: Session, name: str, *, track: str = "cold_colleges", aishe: str | None = None) -> Lead:
    org = Organisation(name=name, org_type="university", state="Kerala", aishe_code=aishe)
    session.add(org)
    session.flush()
    session.add(Contact(org_id=org.id, name=f"Dr. {name.split()[0]} Head", designation="HoD Physics",
                        email=f"hod@{name.split()[0].lower()}-example.ac.in", verified=True))
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == track)),
                stage=session.scalar(select(Stage).where(Stage.name == "New")))
    session.add(lead)
    session.commit()
    return lead


def test_search_finds_leads_contacts_and_colleges(seeded: Session) -> None:
    lead = _lead(seeded, "Sigma University", aishe="U-4242")
    seeded.add(Organisation(name="Sigma Degree College", org_type="college", state="Goa"))
    seeded.commit()
    hits = search_everything(seeded, "sigma")
    kinds = {(h.kind, h.title) for h in hits}
    assert ("lead", "Sigma University") in kinds
    assert ("org", "Sigma Degree College") in kinds  # not a lead yet → opens Organisations
    assert any(h.kind == "contact" and h.lead_id == lead.id for h in hits)  # a contact opens their lead
    assert [h.title for h in search_everything(seeded, "U-4242") if h.kind == "lead"] == ["Sigma University"]
    assert search_everything(seeded, "s") == []  # too short


def test_search_treats_wildcards_as_text(seeded: Session) -> None:
    _lead(seeded, "Tau University")
    assert search_everything(seeded, "%%") == []
    assert search_everything(seeded, "__") == []


def test_task_frame_has_designation_and_step(seeded: Session) -> None:
    lead = _lead(seeded, "Upsilon University")
    start_cadence(seeded, [lead.id], _user(seeded, "Riya"))
    seeded.commit()
    frame = task_frame(seeded, owner_id=None)
    row = frame[frame["lead_id"] == lead.id].iloc[0]
    assert row["Designation"] == "HoD Physics"
    assert row["Step"] == "Step 1 of 4"


def test_alerts_include_handoffs_and_late_queries(seeded: Session) -> None:
    riya, kabir = _user(seeded, "Riya"), _user(seeded, "Kabir")
    task = create_task(seeded, title="Schedule a demo", day=date.today(), owner_id=kabir.id, user=kabir)
    hand_off(seeded, task.id, riya.id, "They want next week", kabir)
    create_query(seeded, NewQuery(channel="Website", received_at=datetime.now(IST) - timedelta(days=2),
                                  name="Dr. Phi", email="phi@phi-example.ac.in", new_org_name="Phi Institute",
                                  owner_id=riya.id), riya)
    seeded.commit()
    counts = shell_counts(seeded, riya.id, riya.name)
    kinds = [a.kind for a in counts.alerts]
    assert "handoff" in kinds and "query" in kinds
    handoff = next(a for a in counts.alerts if a.kind == "handoff")
    assert "Kabir" in handoff.detail
    assert counts.late_queries == 1
    assert counts.due_now >= 1
    # Kabir handed it away, so it is no longer hers.
    assert not any(a.kind == "handoff" for a in shell_counts(seeded, kabir.id, kabir.name).alerts)


def test_week_summary_counts_done_and_planned(seeded: Session) -> None:
    riya = _user(seeded, "Riya")
    lead = _lead(seeded, "Chi University")
    start_cadence(seeded, [lead.id], riya)
    seeded.commit()
    kabir = _user(seeded, "Kabir")
    email = next(a for a in seeded.scalars(select(Activity).where(Activity.lead_id == lead.id))
                 if a.activity_type.channel == "email")
    complete_activity(seeded, email, kabir)
    seeded.commit()
    week = week_summary(seeded, kabir.id)
    assert week["done"] == 1
    assert week["planned"] >= week["done"]
    assert week_summary(seeded, None)["done"] >= 1


def test_period_bounds_follow_indian_fy() -> None:
    today = date(2026, 10, 20)  # a Tuesday
    week = period_bounds("This week", today)
    assert week.start.date() == date(2026, 10, 19) and week.prev_start.date() == date(2026, 10, 12)
    assert (week.end - week.start) == (week.prev_end - week.prev_start)  # same number of days compared
    assert period_bounds("This quarter", today).start.date() == date(2026, 10, 1)
    assert period_bounds("This FY", today).start.date() == date(2026, 4, 1)
    assert period_bounds("This FY", date(2027, 2, 1)).start.date() == date(2026, 4, 1)
    assert period_bounds("This month", date(2026, 3, 15)).prev_start.date() == date(2026, 2, 1)


def test_dashboard_metrics_run(seeded: Session) -> None:
    riya = _user(seeded, "Riya")
    lead = _lead(seeded, "Psi University")
    start_cadence(seeded, [lead.id], riya)
    seeded.commit()
    period = period_bounds("This week")
    rows = {r["label"]: r for r in kpis(seeded, period)}
    assert rows["New leads"]["now"] >= 1
    assert set(rows) == {"New leads", "Outreach done", "Replies", "Demos booked", "Quotations sent"}
    series = weekly_series(seeded, weeks=4)
    assert all(len(v) == 4 for v in series.values())
    team = team_activity(seeded, period)
    assert "Riya Sharma" in set(team["Person"])
    assert list(upcoming_demos(seeded).columns) == ["When", "What", "Organisation", "Who", "lead_id"]


def test_html_pieces_escape_text() -> None:
    assert "&lt;b&gt;" in chip("<b>x</b>")
    assert esc('a"b') == "a&quot;b"
    assert chips(["", ("NQM", "yellow")]).count('class="qa-chip"') == 1  # blanks are skipped
    assert initials("Dr. Ananya Rao") == "AR"
    assert initials("Prof. Kiran") == "K"
    html = stage_stepper(["New", "Replied", "Won"], "Replied")
    assert 'class="done"' in html and 'class="current"' in html and "step 2 of 3" in html
    assert "not started" in stage_stepper(["New"], None)


def test_sign_in_falls_back_to_dev_email_without_auth(monkeypatch) -> None:
    from app.services import auth

    monkeypatch.setenv("DEMO_MODE", "0")  # the non-demo sign-in path
    monkeypatch.setattr(auth, "auth_configured", lambda: False)
    monkeypatch.setenv("DEV_USER_EMAIL", "riya@demo.example")
    assert auth.signed_in_email() == "riya@demo.example"
    # Once sign-in is configured, DEV_USER_EMAIL is ignored (nobody is signed in during tests).
    monkeypatch.setattr(auth, "auth_configured", lambda: True)
    assert auth.signed_in_email() is None


def test_demo_sign_in_uses_the_chosen_persona(monkeypatch) -> None:
    from app.demo import personas
    from app.services import auth

    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr(personas, "chosen_email", lambda: "kabir@demo.example")
    assert auth.signed_in_email() == "kabir@demo.example"
    monkeypatch.setattr(personas, "chosen_email", lambda: None)
    assert auth.signed_in_email() is None  # nobody picked yet: the welcome page shows
