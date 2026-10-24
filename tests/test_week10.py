"""Week 10: GTM charts — the outline language, the automatic GTM map from cadences (with live counts), templates,
rendering to PNG/SVG/PDF, the institute-category comparison and saved diagrams."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Activity, ActivityType, CadenceStep, Lead, Organisation, Stage, Track, User
from app.services.diagrams import (
    TEMPLATES,
    DiagramError,
    cadence_outline,
    comparison_chart,
    delete_diagram,
    diagrams_frame,
    gtm_outline,
    layout,
    parse_outline,
    period_range,
    render,
    render_outline,
    save_diagram,
    segment_comparison,
)


def _user(session: Session, name: str = "Riya") -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def test_outline_language() -> None:
    chart = parse_outline("""# Example map
> summary
## 1. Colleges
Colleges -> Call [Phone] {3 at this step · 1 overdue} -> Break-up email
note: A week apart
## 5. Schools
Schools:
  - Senior secondary -> Email -> Call / WhatsApp
  - Secondary -> Email
hub: Demo [platforms]
  - Reseller | sells in its region
  - Brightpath
""")
    assert chart.title == "Example map" and chart.subtitle == "summary"
    flow, tree, hub = chart.sections
    assert flow.title == "1. Colleges" and flow.note == "A week apart"
    middle = flow.rows[0][1]
    assert (middle.label, middle.sub, middle.badge) == ("Call", "Phone", "3 at this step · 1 overdue")
    assert tree.kind == "tree" and tree.root.label == "Schools" and len(tree.rows) == 2
    assert hub.kind == "hub" and hub.root.sub == "platforms"
    assert [r[0].label for r in hub.rows] == ["Reseller", "Brightpath"] and hub.rows[0][0].sub == "sells in its region"
    lay = layout(chart)
    assert len(lay.boxes) == 3 + (1 + 3 + 2) + (1 + 2) and lay.height > 5
    with pytest.raises(DiagramError, match="Line 1"):
        parse_outline("A -> -> B")
    with pytest.raises(DiagramError, match="indented"):
        parse_outline("  - orphan")
    with pytest.raises(DiagramError, match="Nothing to draw"):
        parse_outline("# Only a title")


def test_rendering_formats() -> None:
    for name, text in TEMPLATES.items():
        png = render_outline(text, "png")
        assert png[:8] == b"\x89PNG\r\n\x1a\n", name
    chart = parse_outline(TEMPLATES["reseller"])
    assert render(chart, "svg").lstrip().startswith(b"<?xml")
    assert render(chart, "pdf")[:4] == b"%PDF"
    with pytest.raises(DiagramError):
        render(chart, "gif")


def test_gtm_map_follows_cadences_with_live_counts(seeded: Session) -> None:
    track = seeded.scalar(select(Track).where(Track.code == "cold_colleges"))
    org = Organisation(name="Omega University", org_type="university")
    seeded.add(org)
    seeded.flush()
    lead = Lead(org_id=org.id, track_id=track.id, stage=seeded.scalar(select(Stage).where(Stage.name == "New")),
                cadence_status="active", current_step_no=1)
    seeded.add(lead)
    seeded.flush()
    step = seeded.scalar(select(CadenceStep).where(CadenceStep.track_id == track.id, CadenceStep.step_no == 1))
    seeded.add(Activity(type_id=seeded.scalar(select(ActivityType.id).where(ActivityType.name == "Email")),
                        lead_id=lead.id, org_id=org.id, title="Initial", status="planned", cadence_step_id=step.id,
                        scheduled_at=datetime.now(timezone.utc) - timedelta(days=9)))
    seeded.commit()
    text = gtm_outline(seeded)
    assert text.startswith("# GTM map")
    assert f"## 1. {track.name}" in text
    assert "{1 at this step · 1 overdue}" in text
    assert "Email + LinkedIn · day 0" in text and "+4–6 days" in text
    csr = seeded.scalar(select(Track).where(Track.code == "schools"))
    if csr is not None and csr.sub_tiers:
        assert f"{csr.name}:" in text and f"  - {csr.sub_tiers[0]} ->" in text   # sub-tiers become branches
    chart = parse_outline(text)
    assert len(chart.sections) >= 6
    assert "{" not in gtm_outline(seeded, live=False)
    one = cadence_outline(seeded, track.id)
    assert one.startswith(f"# {track.name} — cadence") and "live counts" not in one


def test_segment_comparison_and_chart(seeded: Session) -> None:
    track = seeded.scalar(select(Track.id).where(Track.code == "cold_colleges"))
    new = seeded.scalar(select(Stage).where(Stage.name == "New"))
    for name, management, area in (("A Univ", "State Government", "Urban"), ("B College", "Private Un-Aided", "Rural"),
                                   ("C College", None, None)):
        org = Organisation(name=name, org_type="college", management=management, area=area)
        seeded.add(org)
        seeded.flush()
        seeded.add(Lead(org_id=org.id, track_id=track, stage=new))
    seeded.commit()
    start, end = period_range("This FY", date(2026, 10, 21))
    assert start == date(2026, 4, 1)
    frame = segment_comparison(seeded, start, end)
    assert list(frame["Category"]) == ["Public · Urban", "Private · Rural", "Not set"]
    assert list(segment_comparison(seeded, start, end, "sector")["Category"]) == ["Public", "Private", "Not set"]
    png = comparison_chart(frame, ["Leads", "Won value (₹)", "Win rate"], "Test")
    assert png[:4] == b"\x89PNG"


def test_saved_diagrams(seeded: Session) -> None:
    me, kabir = _user(seeded), _user(seeded, "Kabir")
    diagram = save_diagram(seeded, title="Reseller model", outline=TEMPLATES["reseller"], user=me, kind="reseller")
    seeded.commit()
    assert diagrams_frame(seeded)["Title"].tolist() == ["Reseller model"]
    with pytest.raises(DiagramError):
        save_diagram(seeded, title="Broken", outline="A -> ", user=me)
    with pytest.raises(DiagramError):
        save_diagram(seeded, title="", outline=TEMPLATES["blank"], user=me)
    save_diagram(seeded, title="Reseller model v2", outline=TEMPLATES["reseller"], user=me, diagram_id=diagram.id)
    seeded.commit()
    assert diagrams_frame(seeded)["Title"].tolist() == ["Reseller model v2"]
    with pytest.raises(PermissionError):
        delete_diagram(seeded, diagram.id, kabir)
    delete_diagram(seeded, diagram.id, me)
    seeded.commit()
    assert diagrams_frame(seeded).empty


def test_alt_text_describes_the_diagram() -> None:
    from app.services.diagrams import describe

    text = describe(parse_outline(TEMPLATES["partner"]))
    assert text.startswith("Partner models") and "Demo at the centre, linked to: Reseller" in text
    assert "Reseller → Steady sales → Distribution partner" in describe(parse_outline(TEMPLATES["reseller"]))
