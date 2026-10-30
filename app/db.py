"""Database engine and sessions."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_database_url


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            # Streamlit serves each visitor on its own thread; SQLite must allow that, and wait briefly for locks.
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")   # SQLite ignores foreign keys unless asked
            cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the one writer
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine
    return create_engine(
        url,
        # Supabase's pooler drops idle connections; pre_ping reconnects instead of erroring.
        pool_pre_ping=True,
        # Free tier allows few connections; a small pool keeps us well under the limit.
        pool_size=3,
        max_overflow=2,
        pool_recycle=300,
    )


# ---------------------------------------------------------------- data generation
# A number that goes up whenever anything is saved (a commit that wrote rows) or the demo data is swapped. The app
# compares it on every run and clears its caches when it moved, so pages can cache their tables for an hour and
# still never show something older than the last save — whoever made it, on whichever page.
_generation = 0
_generation_lock = threading.Lock()


def data_generation() -> int:
    return _generation


def bump_generation() -> None:
    global _generation
    with _generation_lock:
        _generation += 1


@event.listens_for(Session, "after_flush")
def _note_write(session: Session, _context) -> None:
    session.info["wrote"] = True  # a flush only fires when rows were actually inserted, changed or deleted


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    if session.info.pop("wrote", False):
        bump_generation()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("wrote", None)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine(get_database_url())


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def reset_engine() -> None:
    """Drops pooled connections so the next query opens the (newly rebuilt) demo database."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    _session_factory.cache_clear()
    bump_generation()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit if the block succeeds, roll back if it raises — so half-saved records never happen."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
