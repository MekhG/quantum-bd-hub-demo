"""Guards for the portfolio demo: the generated world is complete, current, and contains nothing real."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 22)

# Names and identifiers of the real company the hub was first built for, its partners and colleagues. None may
# appear in the demo's code or docs. (The author's own name is allowed there as the credit — but never in the data.)
_REAL = (r"quacademy|thequantum|qulabs|quantum ai global|\bwiser\b|\bargo\b|hitesh|gopika|\btanvi\b|\bsruthi\b"
         r"|\banand\b|alliance university|q-cluster|qntsim|q-compute|36aaacq|may@"
         # the real go-to-market: offer names, chart title and positioning
         r"|q-build|q-learn|current gtm in place|phase-wise|universities – fresh|hitesh|state programs|csr companies"
         r"|\buni_fresh\b|\bstate_orgs\b|\bwarm_leads\b|initial outreach|fresh leads")
FORBIDDEN = re.compile(_REAL, re.IGNORECASE)
FORBIDDEN_IN_DATA = re.compile(_REAL + r"|mekhala|ganguly", re.IGNORECASE)


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> str:
    from app.demo.build import build

    path = tmp_path_factory.mktemp("demo") / "demo.db"
    url = f"sqlite:///{path.as_posix()}"
    build(url, today=TODAY, quiet=True)  # the full twelve weeks, as deployed
    return url


def _text_values(url: str):
    engine = create_engine(url)
    insp = inspect(engine)
    with engine.connect() as conn:
        for table in insp.get_table_names():
            for column in insp.get_columns(table):
                if str(column["type"]).upper().startswith(("VARCHAR", "TEXT", "JSON", "CHAR")):
                    for (value,) in conn.execute(text(f'select "{column["name"]}" from "{table}"')):
                        if value:
                            yield table, column["name"], str(value)
    engine.dispose()


def test_no_real_names_or_contact_details_in_the_data(demo_db: str) -> None:
    hits = [(t, c, v[:80]) for t, c, v in _text_values(demo_db) if FORBIDDEN_IN_DATA.search(v)]
    assert not hits, hits[:5]


def test_every_email_is_on_a_reserved_domain_and_phones_cannot_ring(demo_db: str) -> None:
    engine = create_engine(demo_db)
    with Session(engine) as s:
        emails = [e for (e,) in s.execute(text(
            "select email from contacts where email is not null union all select email from users "
            "union all select email from community_members"))]
        phones = [p for (p,) in s.execute(text("select phone from contacts where phone is not null"))]
    engine.dispose()
    assert emails and all(e.endswith(".example") for e in emails), [e for e in emails if not e.endswith(".example")]
    # Indian mobiles start with 6–9; '+91 0…' cannot reach anyone.
    assert phones and all(re.sub(r"\D", "", p).startswith("910") for p in phones)


def test_the_story_ends_today_with_work_to_do(demo_db: str) -> None:
    engine = create_engine(demo_db)
    with Session(engine) as s:
        due_today = s.execute(text(
            "select count(*) from activities where status='planned' and date(scheduled_at)=:d"), {"d": str(TODAY)}).scalar()
        latest = s.execute(text("select max(date(occurred_at)) from activities where status='done'")).scalar()
        tracks_with_leads = s.execute(text("select count(distinct track_id) from leads")).scalar()
        posts = dict(s.execute(text("select status, count(*) from content_posts group by status")).all())
        finals = s.execute(text("select count(*) from weekly_reports where status='final'")).scalar()
        pending_regs = s.execute(text("select count(*) from deal_registrations where status='pending'")).scalar()
        hot = s.execute(text("select count(*) from leads where score >= 40")).scalar()
    engine.dispose()
    assert due_today >= 5
    assert latest in (str(TODAY), str(TODAY - timedelta(days=1)))
    assert tracks_with_leads == 7
    assert {"posted", "review", "approved", "idea"} <= set(posts)
    assert finals >= 3 and pending_regs == 1 and hot >= 1


def test_the_same_day_builds_the_same_world(tmp_path) -> None:
    from app.demo.build import build

    a = build(f"sqlite:///{(tmp_path / 'a.db').as_posix()}", today=TODAY, days=14, quiet=True)
    b = build(f"sqlite:///{(tmp_path / 'b.db').as_posix()}", today=TODAY, days=14, quiet=True)
    a.pop("seconds"), b.pop("seconds")
    assert a == b


def test_the_code_and_docs_carry_no_real_company_details() -> None:
    skip = {"test_demo_data.py"}
    offenders = []
    for path in ROOT.rglob("*"):
        if path.is_dir() or path.name in skip or any(part in (".git", ".venv", "data", "__pycache__", ".pytest_cache")
                                                     for part in path.parts):
            continue
        if path.suffix.lower() not in (".py", ".md", ".toml", ".txt", ".csv", ".ini", ".mako", ".yml", ".yaml"):
            continue
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}")
    assert not offenders, "\n".join(offenders[:20])
