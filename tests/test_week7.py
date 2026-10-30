"""Week 7: news (feed reading, tagging, institution matching, actions) and signals (adding, fading, the lead score)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityType,
    Document,
    Lead,
    NewsItem,
    NewsSource,
    Opportunity,
    Organisation,
    OrgSignal,
    Quote,
    SignalType,
    Stage,
    Track,
    User,
)
from app.services.news import (
    NewsError,
    OrgMatcher,
    TypeSuggester,
    feed_url,
    fetch_news,
    news_items,
    parse_feed,
    rematch,
    save_as_signal,
    save_org_aliases,
    save_sources,
    save_to_library,
    set_status,
    sources_frame,
    unread_lead_news,
)
from app.services.signals import (
    SignalError,
    add_signal,
    bulk_add,
    hot_leads,
    match_pasted_names,
    name_variants,
    points_now,
    recompute_scores,
    remove_signal,
    save_types,
    score_parts,
    signals_frame,
    types_frame,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 20)


def _user(session: Session, name: str = "Riya") -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _lead(session: Session, name: str, *, track: str = "cold_colleges", aliases: list[str] | None = None,
          stage: str = "New") -> Lead:
    org = Organisation(name=name, org_type="university", state="Kerala", aliases=aliases)
    session.add(org)
    session.flush()
    lead = Lead(org_id=org.id, track_id=session.scalar(select(Track.id).where(Track.code == track)),
                stage=session.scalar(select(Stage).where(Stage.name == stage)), channel="Direct")
    session.add(lead)
    session.commit()
    return lead


def _type(session: Session, name: str) -> SignalType:
    return session.scalar(select(SignalType).where(SignalType.name == name))


def _fake_fetch(mapping: dict[str, str]):
    def fetch(url: str) -> bytes:
        for needle, file in mapping.items():
            if needle in url:
                return (FIXTURES / file).read_bytes()
        raise ConnectionError("no route in test")
    return fetch


def _only_sources(session: Session, keep: list[str]) -> None:
    for source in session.scalars(select(NewsSource)):
        source.is_active = source.name in keep
    session.commit()


# ---------------------------------------------------------------- reading feeds


def test_parse_google_news_strips_publisher_and_repeated_summary() -> None:
    items = parse_feed((FIXTURES / "google_news_nqm.xml").read_bytes())
    first = items[0]
    assert first.title == "Omega University gets approval for NQM teaching lab"
    assert first.publisher == "Example Times"
    assert first.summary is None  # Google's summary only repeats the headline
    assert first.published_at == datetime(2026, 10, 19, 7, 30, tzinfo=timezone.utc)
    pib = parse_feed((FIXTURES / "pib.xml").read_bytes())
    assert pib[1].summary.startswith("The Department of Science and Technology")
    with pytest.raises(NewsError):
        parse_feed(b"this is not a feed")


def test_feed_url_uses_indian_edition_and_last_30_days(seeded: Session) -> None:
    source = seeded.scalar(select(NewsSource).where(NewsSource.name == "National Quantum Mission"))
    url = feed_url(source)
    assert url.startswith("https://news.google.com/rss/search?q=")
    assert "hl=en-IN&gl=IN&ceid=IN:en" in url and "when%3A30d" in url
    pib = seeded.scalar(select(NewsSource).where(NewsSource.kind == "rss", NewsSource.name.like("PIB%")))
    assert feed_url(pib) == pib.url


def test_fetch_stores_tags_matches_and_skips_duplicates(seeded: Session) -> None:
    omega = _lead(seeded, "Omega University")
    kalpaka = _lead(seeded, "Indian Institute of Technology Kalpaka")
    _lead(seeded, "Government Degree College")  # generic name: must never be matched
    _only_sources(seeded, ["National Quantum Mission", "NQM teaching labs", "PIB press releases",
                           "Quantum jobs in India"])
    fetch = _fake_fetch({"National+Quantum+Mission": "google_news_nqm.xml", "teaching+lab": "google_news_labs.xml",
                         "pib.gov.in": "pib.xml"})
    result = fetch_news(seeded, fetch=fetch, now=NOW)
    seeded.commit()
    assert result.checked == 4
    assert len(result.errors) == 1 and result.errors[0].startswith("Quantum jobs in India")
    titles = {i.title: i for i in seeded.scalars(select(NewsItem))}
    # Same URL from two searches and the same headline from another publisher are stored once; the 2025 story and
    # the PIB release that isn't about quantum are left out.
    assert sum(t.startswith("Omega University gets approval") for t in titles) == 1
    assert "A very old story about quantum" not in titles
    assert "Cabinet approves new highway projects" not in titles
    assert len(titles) == 6

    omega_item = titles["Omega University gets approval for NQM teaching lab"]
    assert omega_item.matched_org_ids == [omega.org_id]
    assert omega_item.suggested_type_id == _type(seeded, "NQM lab approved").id
    assert omega_item.topic == "NQM & policy"
    tender = titles["IIT Kalpaka invites tender for quantum computing software"]
    assert tender.matched_org_ids == [kalpaka.org_id]  # 'IIT Kalpaka' is the short form of the full name
    assert tender.suggested_type_id == _type(seeded, "Tender / RFQ").id
    assert titles["Government Degree College hosts quantum awareness talk"].matched_org_ids is None
    coe = titles["Sigma Institute of Technology launches centre of excellence in quantum"]
    assert coe.suggested_type_id == _type(seeded, "New department / centre of excellence").id
    assert coe.matched_org_ids is None  # not a lead (yet)

    sources = sources_frame(seeded).set_index("Name")
    assert sources.loc["Quantum jobs in India", "Problem"].startswith("ConnectionError")
    assert sources.loc["National Quantum Mission", "New last time"] == 5 - 1  # the old story is skipped
    # Not due again until the refresh interval has passed.
    assert fetch_news(seeded, fetch=fetch, now=NOW + timedelta(hours=1)).checked == 0
    assert fetch_news(seeded, fetch=fetch, now=NOW + timedelta(hours=7)).checked == 4


def test_matcher_rules() -> None:
    matcher = OrgMatcher([
        (1, "Indian Institute of Science (IISc)", []),
        (2, "Andhra University, Visakhapatnam", []),
        (3, "Government College", []),
        (4, "St. Joseph's College", ["SJC Bengaluru"]),
        (5, "St. Joseph's College", []),          # same name twice → ambiguous, never matched by name
        (6, "Indian Institute of Science Education and Research Pune", []),
    ])
    assert matcher.find("IISc researchers build a quantum sensor") == [1]
    assert matcher.find("Quantum workshop at Andhra University this week") == [2]
    assert matcher.find("Government College students visit lab") == []
    assert matcher.find("St. Joseph's College opens a lab") == []
    assert matcher.find("SJC Bengaluru opens a lab") == [4]
    assert matcher.find("IISER Pune and IISc sign MoU") == [6, 1]
    assert "iit bombay" in name_variants("Indian Institute of Technology Bombay")


def test_type_suggester_prefers_higher_points_and_whole_words(seeded: Session) -> None:
    suggester = TypeSuggester(seeded.scalars(select(SignalType)))
    tender = _type(seeded, "Tender / RFQ").id
    assert suggester.suggest("University floats tender; grant of 5 crore") == tender  # 30 points beats 15
    assert suggester.suggest("Bidding farewell to the old lab") is None  # 'bid' must be a whole word
    assert suggester.suggest("Minister inaugurates quantum centre") == \
        _type(seeded, "New department / centre of excellence").id  # 'inaugurat*' is a prefix


# ---------------------------------------------------------------- signals and the score


def test_points_fade_in_a_straight_line() -> None:
    assert points_now(30, 12, TODAY, TODAY) == 30
    assert points_now(30, 12, TODAY - timedelta(days=183), TODAY) == 15
    assert points_now(30, 12, TODAY - timedelta(days=366), TODAY) == 0
    assert points_now(30, 12, TODAY + timedelta(days=3), TODAY) == 30


def test_score_adds_signals_once_per_type_and_auto_points(seeded: Session) -> None:
    me = _user(seeded)
    lead = _lead(seeded, "Omega University")
    nqm_lab, funding = _type(seeded, "NQM lab approved"), _type(seeded, "Funding / grant")
    today = date.today()
    add_signal(seeded, org_id=lead.org_id, type_id=nqm_lab.id, observed_on=today, user=me)
    add_signal(seeded, org_id=lead.org_id, type_id=funding.id, observed_on=today - timedelta(days=10), user=me)
    _, created = add_signal(seeded, org_id=lead.org_id, type_id=funding.id, observed_on=today - timedelta(days=40),
                            user=me, source_url="https://example.test/older")
    assert created
    _, again = add_signal(seeded, org_id=lead.org_id, type_id=funding.id, observed_on=today - timedelta(days=40),
                          user=me, source_url="https://example.test/older")
    assert not again  # the same signal twice is not duplicated
    lead.nqm_flag = True
    inbound = seeded.scalar(select(ActivityType).where(ActivityType.name == "Website enquiry"))
    seeded.add(Activity(type_id=inbound.id, lead_id=lead.id, org_id=lead.org_id, title="Asked for pricing",
                        status="done", direction="inbound",
                        occurred_at=datetime.now(timezone.utc) - timedelta(days=1)))
    seeded.commit()
    recompute_scores(seeded)
    seeded.commit()
    parts = score_parts(seeded, lead.id)
    by_name = {}
    for p in parts:
        by_name.setdefault(p.signal, []).append(p.points)
    assert by_name["NQM lab approved"] == [30]
    assert sorted(by_name["Funding / grant"], reverse=True)[1] == 0  # only the latest funding signal counts
    assert by_name["NQM applicant"] == [20]
    assert 14 <= by_name["Replied recently"][0] <= 15
    expected = 30 + by_name["Funding / grant"][0] + 20 + by_name["Replied recently"][0]
    assert seeded.get(Lead, lead.id).score == expected
    assert hot_leads(seeded)["Organisation"].tolist() == ["Omega University"]

    # Automatic types can't be added by hand; future dates are refused.
    with pytest.raises(SignalError):
        add_signal(seeded, org_id=lead.org_id, type_id=_type(seeded, "Demo held").id, observed_on=today, user=me)
    with pytest.raises(SignalError):
        add_signal(seeded, org_id=lead.org_id, type_id=nqm_lab.id, observed_on=today + timedelta(days=2), user=me)

    # Removing: only the person who added it or an admin.
    neha = _user(seeded, "Neha")
    signal_id = seeded.scalar(select(OrgSignal.id).where(OrgSignal.signal_type_id == nqm_lab.id))
    with pytest.raises(PermissionError):
        remove_signal(seeded, signal_id, neha)
    remove_signal(seeded, signal_id, me)
    seeded.commit()
    assert seeded.get(Lead, lead.id).score == expected - 30

    # A won lead scores 0.
    lead.stage = seeded.scalar(select(Stage).where(Stage.name == "Deal PO Released"))
    seeded.commit()
    recompute_scores(seeded, lead_ids=[lead.id])
    assert seeded.get(Lead, lead.id).score == 0


def test_open_quote_counts(seeded: Session) -> None:
    lead = _lead(seeded, "Omega University")
    deal = Opportunity(lead_id=lead.id, title="Starter lab", value_inr=Decimal("1100000"))
    seeded.add(deal)
    seeded.flush()
    seeded.add(Quote(opportunity_id=deal.id, number="DEMO/OMU/2026-27/001", status="sent",
                     quote_date=date.today(), valid_until=date.today() + timedelta(days=20),
                     sent_on=date.today()))
    seeded.commit()
    recompute_scores(seeded)
    assert seeded.get(Lead, lead.id).score == 25


def test_signal_lists_and_bulk_paste(seeded: Session) -> None:
    me = _user(seeded)
    omega = _lead(seeded, "Omega University")
    kalpaka = _lead(seeded, "Indian Institute of Technology Kalpaka")
    _lead(seeded, "Sree Narayana Guru College of Engineering", aliases=["SNGCE"])
    matches = match_pasted_names(seeded, "1. Omega University\n2) IIT Kalpaka\n- Sree Narayana Guru Colege of "
                                         "Engineering\n• Zeta Institute\n\n")
    assert [(m.pasted, m.how) for m in matches] == [
        ("Omega University", "Exact"), ("IIT Kalpaka", "Exact"),
        ("Sree Narayana Guru Colege of Engineering", "Close"), ("Zeta Institute", "Not found")]
    assert matches[1].org_id == kalpaka.org_id
    added, already = bulk_add(seeded, [omega.org_id, kalpaka.org_id, omega.org_id],
                              type_id=_type(seeded, "NQM lab approved").id, observed_on=date.today(), user=me,
                              source_url="https://pib.gov.in/PressReleasePage.aspx?PRID=1")
    seeded.commit()
    assert (added, already) == (2, 0)
    frame = signals_frame(seeded)
    assert set(frame["Organisation"]) == {"Omega University", "Indian Institute of Technology Kalpaka"}
    assert frame["Points now"].tolist() == [30, 30]
    assert seeded.get(Lead, omega.id).score == 30


def test_news_actions(seeded: Session) -> None:
    me = _user(seeded)
    omega = _lead(seeded, "Omega University")
    _only_sources(seeded, ["National Quantum Mission"])
    fetch_news(seeded, fetch=_fake_fetch({"National": "google_news_nqm.xml"}), now=datetime.now(timezone.utc))
    seeded.commit()
    items = news_items(seeded, leads_only=True)
    assert [i["title"] for i in items] == ["Omega University gets approval for NQM teaching lab"]
    assert items[0]["matched"] == [(omega.org_id, "Omega University", omega.id)]
    assert items[0]["suggested"] == "NQM lab approved"
    assert unread_lead_news(seeded) == 1

    item_id = items[0]["id"]
    added = save_as_signal(seeded, item_id, org_ids=[omega.org_id], type_id=items[0]["suggested_type_id"],
                           observed_on=date.today(), user=me)
    doc_id = save_to_library(seeded, item_id, me)
    seeded.commit()
    assert added == 1
    assert seeded.get(NewsItem, item_id).status == "signal"
    assert seeded.get(Lead, omega.id).score == 30
    signal = seeded.scalar(select(OrgSignal))
    assert signal.source_url.endswith("TEST-001?oc=5") and signal.news_item_id == item_id
    doc = seeded.get(Document, doc_id)
    assert doc.doc_type == "News clipping" and doc.status == "draft"
    with pytest.raises(NewsError):
        save_to_library(seeded, item_id, me)

    other = next(i for i in news_items(seeded) if i["title"].startswith("Quantum startups"))
    set_status(seeded, other["id"], "dismissed", me)
    seeded.commit()
    assert other["id"] not in {i["id"] for i in news_items(seeded)}
    assert other["id"] in {i["id"] for i in news_items(seeded, statuses=("dismissed",))}
    assert {i["title"] for i in news_items(seeded, text="tender")} == \
        {"IIT Kalpaka invites tender for quantum computing software"}


def test_aliases_and_rematch(seeded: Session) -> None:
    me = _user(seeded)
    lead = _lead(seeded, "Sigma Institute of Technology and Science")
    _only_sources(seeded, ["NQM teaching labs"])
    fetch_news(seeded, fetch=_fake_fetch({"teaching": "google_news_labs.xml"}), now=datetime.now(timezone.utc))
    seeded.commit()
    item = seeded.scalar(select(NewsItem).where(NewsItem.title.like("Sigma%")))
    assert item.matched_org_ids is None
    save_org_aliases(seeded, lead.org_id, "Sigma Institute of Technology, SIT", me)
    assert rematch(seeded) >= 1
    seeded.commit()
    assert seeded.get(NewsItem, item.id).matched_org_ids == [lead.org_id]


def test_configuration_editors(seeded: Session) -> None:
    me = _user(seeded)
    frame = sources_frame(seeded)
    frame.loc[0, "Search words"] = '"National Quantum Mission" labs'
    frame = pd.concat([frame, pd.DataFrame([{"Name": "Quantum sensing", "Kind": "Google News search",
                                             "Search words": "quantum sensing India", "Topic": "Research",
                                             "Active": True}])], ignore_index=True)
    assert save_sources(seeded, frame, me) == 2
    seeded.commit()
    assert seeded.scalar(select(NewsSource).where(NewsSource.name == "Quantum sensing")).topic == "Research"
    bad = sources_frame(seeded)
    bad.loc[0, "Topic"] = ""
    with pytest.raises(NewsError):
        save_sources(seeded, bad, me)
    with pytest.raises(PermissionError):
        save_sources(seeded, sources_frame(seeded), _user(seeded, "Kabir"))

    types = types_frame(seeded)
    auto_row = types.index[types["Automatic"]][0]
    types.loc[auto_row, "Signal"] = "Renamed automatic"   # automatic names are fixed
    types.loc[types["Signal"] == "FDP / workshop", "Points"] = 12
    assert save_types(seeded, types, me) == 1
    seeded.commit()
    assert _type(seeded, "FDP / workshop").weight == 12
    assert _type(seeded, "Renamed automatic") is None
    types.loc[types["Signal"] == "FDP / workshop", "Points"] = 500
    with pytest.raises(SignalError):
        save_types(seeded, types, me)
