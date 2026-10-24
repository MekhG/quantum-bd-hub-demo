"""Shared test setup: a fresh in-memory database with the seeded configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import Base
from app.seed.seed_config import seed_all


def _engine():
    # Optional: TEST_DATABASE_URL points at a THROWAWAY Postgres to run the same checks there.
    # Never set it to the Supabase database — every test wipes the tables.
    test_url = os.environ.get("TEST_DATABASE_URL")
    if test_url:
        engine = create_engine(test_url)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        return engine

    engine = create_engine("sqlite://")

    # SQLite ignores foreign keys unless asked; switch them on so tests match Postgres.
    @event.listens_for(engine, "connect")
    def _enable_fks(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def session() -> Iterator[Session]:
    with Session(_engine()) as s:
        yield s


@pytest.fixture()
def seeded() -> Iterator[Session]:
    with Session(_engine()) as s:
        seed_all(s)
        s.commit()
        yield s
