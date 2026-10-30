"""Builds the demo database: the configuration, then twelve weeks of fictional BD work ending today.

Run:  python -m app.demo.build                      (writes data/demo.db)
      python -m app.demo.build --out other.db       (another SQLite file)
      python -m app.demo.build --url postgresql://… (a throwaway Postgres — every table is wiped first)

How it works: a clock (freezegun) is moved day by day through the past twelve weeks while the story in
app/demo/story.py calls the hub's own services — importing lists, starting cadences, finishing tasks, logging replies,
sending quotes, approving posts. Everything the app shows (timelines, stage history, reports, scores) is therefore
exactly what the real app would have produced. The run is seeded, so the same day always gives the same data.
"""

from __future__ import annotations

import argparse
import random
import sys
import time as clock_time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import DEMO_DB_PATH, normalise_url
from app.models import Base
from app.models.base import TZDateTime

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DAYS = 84  # twelve weeks of history
_STAMPED = ("created_at", "updated_at", "changed_at", "added_at", "imported_at", "fetched_at", "captured_on",
            "received_on")


class Clock:
    """Moves 'now' for the whole app while the story runs (date.today(), datetime.now(), today_ist()…)."""

    def __init__(self, start: datetime) -> None:
        from freezegun import freeze_time

        self._freezer = freeze_time(start)
        self._frozen = self._freezer.start()

    def at(self, day: date, hour: int = 10, minute: int = 0) -> datetime:
        moment = datetime.combine(day, time(hour, minute), tzinfo=IST)
        self._frozen.move_to(moment)
        return moment

    def stop(self) -> None:
        self._freezer.stop()


def _stamp_defaults() -> None:
    """Columns the database normally fills (created_at = CURRENT_TIMESTAMP…) would get the real clock time, not the
    story's. Fill them from the moved clock before every insert/update instead."""

    def _now_for(column):
        if isinstance(column.type, Date) and not isinstance(column.type, (DateTime, TZDateTime)):
            return datetime.now(IST).date()
        return datetime.now(timezone.utc)

    @event.listens_for(Base, "before_insert", propagate=True)
    def _before_insert(mapper, _connection, target) -> None:
        for column in mapper.columns:
            if column.key in _STAMPED and getattr(target, column.key, None) is None:
                setattr(target, column.key, _now_for(column))

    @event.listens_for(Base, "before_update", propagate=True)
    def _before_update(mapper, _connection, target) -> None:
        if "updated_at" in mapper.columns:
            target.updated_at = datetime.now(timezone.utc)


def _preload() -> None:
    """Import date-aware libraries before the clock moves: a module first imported while the clock is frozen keeps
    the fake datetime class for good (harmless in the build's own process, wrong if the caller carries on)."""
    import openpyxl  # noqa: F401
    import openpyxl.cell.cell  # noqa: F401
    import pandas  # noqa: F401
    from docx import Document  # noqa: F401

    from app.services import privacy_requests, report_docx, report_metrics  # noqa: F401


def _copy_to_file(engine, path: Path) -> None:
    """Writes the in-memory database to `path` with SQLite's own backup (a consistent, page-by-page copy)."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    raw = engine.raw_connection()
    try:
        destination = sqlite3.connect(path)
        try:
            raw.driver_connection.backup(destination)
        finally:
            destination.close()
    finally:
        raw.close()


def build(url: str, *, today: date, days: int = DEFAULT_DAYS, seed: int = 2026, quiet: bool = False) -> dict:
    from app.demo.story import Story

    _preload()

    started = clock_time.monotonic()
    # A SQLite file is built in memory and copied to disk at the end: thousands of small writes never touch the
    # disk (cloud volumes can be slow). Postgres is written to directly.
    target = url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else None
    engine = create_engine("sqlite://", poolclass=StaticPool) if target else create_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fks(dbapi_connection, _record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _stamp_defaults()

    first_day = today - timedelta(days=days)
    clock = Clock(datetime.combine(first_day - timedelta(days=400), time(9, 0), tzinfo=IST))
    try:
        with Session(engine, expire_on_commit=False) as session:
            story = Story(session, clock=clock, today=today, first_day=first_day, rng=random.Random(seed))
            summary = story.run(log=None if quiet else print)
            session.commit()
    finally:
        clock.stop()
    if target:
        _copy_to_file(engine, Path(target))
    engine.dispose()
    summary["seconds"] = round(clock_time.monotonic() - started, 1)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the demo database with fictional data.")
    parser.add_argument("--out", help="SQLite file to write (default data/demo.db)")
    parser.add_argument("--url", help="A database URL instead of a SQLite file (all tables are wiped)")
    parser.add_argument("--today", help="The story ends on this date (YYYY-MM-DD; default today in IST)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else datetime.now(IST).date()
    if args.url:
        url = normalise_url(args.url)
    else:
        path = Path(args.out) if args.out else DEMO_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        url = f"sqlite:///{path.resolve().as_posix()}"
    summary = build(url, today=today, days=args.days, quiet=args.quiet)
    print("Demo data built:", ", ".join(f"{k} {v}" for k, v in summary.items()))


if __name__ == "__main__":
    sys.exit(main())
