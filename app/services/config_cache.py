"""Remembers configuration rows (cadence steps, stages, activity types, task assignments, the team) for the length of one
database session, so a busy operation — finishing a hundred tasks, a large import — doesn't look the same rows up
again for every lead.

Sessions in the app last one page run, so a change made on the Configuration page is always seen by the next run.
Inside a session the memory is dropped the moment any configuration row is added, changed or deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import ActivityType, CadenceStep, Stage, TaskAssignment, Track, User

T = TypeVar("T")
_CONFIG_CLASSES = (ActivityType, CadenceStep, Stage, TaskAssignment, Track, User)
_KEY = "config_memo"


def remembered(session: Session, key: Any, load: Callable[[], T]) -> T:
    memo: dict = session.info.setdefault(_KEY, {})
    if key not in memo:
        memo[key] = load()
    return memo[key]


@event.listens_for(Session, "before_flush")
def _forget_on_config_change(session: Session, _context, _instances) -> None:
    if _KEY not in session.info:
        return
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, _CONFIG_CLASSES):
            session.info.pop(_KEY, None)
            return


@event.listens_for(Session, "after_rollback")
def _forget_on_rollback(session: Session) -> None:
    session.info.pop(_KEY, None)
