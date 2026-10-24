"""Cadence settings the admin changes on the Configuration page: auto-start, daily cap, who gets which tasks."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import ActivityType, AuditLog, CadenceStep, TaskAssignment, Track, User

LEAD_OWNER = "Lead owner"


def _require_admin(user: User | None) -> None:
    if user is None or user.role != "admin":
        raise PermissionError("Only an admin can change cadence settings.")


def settings_frame(session: Session) -> pd.DataFrame:
    rows = []
    for track in session.scalars(select(Track).where(Track.is_active.is_(True)).order_by(Track.sort_order)
                                 .options(selectinload(Track.cadence_steps).selectinload(CadenceStep.activity_types))):
        steps = " → ".join(
            f"{s.name} ({' + '.join(t.name for t in s.activity_types)}"
            + (f", +{s.days_after_previous}" + (f"–{s.days_after_previous + s.grace_days}" if s.grace_days else "")
               + "d" if s.step_no > 1 else "") + ")"
            for s in track.cadence_steps
        ) or "No cadence"
        rows.append({"track_id": track.id, "Track": track.name, "Steps": steps,
                     "Auto-start when verified": track.auto_start_cadence,
                     "Daily start cap": track.daily_start_cap})
    return pd.DataFrame(rows).set_index("track_id") if rows else pd.DataFrame()


def save_track_settings(session: Session, edited: pd.DataFrame, user: User) -> int:
    _require_admin(user)
    changed = 0
    for track_id, row in edited.iterrows():
        track = session.get(Track, int(track_id))
        if track is None:
            continue
        auto = bool(row["Auto-start when verified"])
        cap_value = row["Daily start cap"]
        cap = None if cap_value is None or pd.isna(cap_value) or int(cap_value) <= 0 else int(cap_value)
        diff = {}
        if track.auto_start_cadence != auto:
            diff["Auto-start when verified"] = [track.auto_start_cadence, auto]
            track.auto_start_cadence = auto
        if track.daily_start_cap != cap:
            diff["Daily start cap"] = [track.daily_start_cap, cap]
            track.daily_start_cap = cap
        if diff:
            session.add(AuditLog(user_id=user.id, entity="tracks", entity_id=str(track.id), action="update",
                                 diff=diff))
            changed += 1
    session.flush()
    return changed


def assignment_frame(session: Session) -> pd.DataFrame:
    """One row per track × task type used in its cadence (plus any rule already saved)."""
    rules = {(a.track_id, a.activity_type_id): a.user_id for a in session.scalars(select(TaskAssignment))}
    users = {u.id: u.name for u in session.scalars(select(User))}
    rows = []
    for track in session.scalars(select(Track).where(Track.is_active.is_(True)).order_by(Track.sort_order)
                                 .options(selectinload(Track.cadence_steps).selectinload(CadenceStep.activity_types))):
        type_ids: dict[int, str] = {}
        for step in track.cadence_steps:
            for kind in step.activity_types:
                type_ids[kind.id] = kind.name
        for (rule_track, rule_type), _ in rules.items():
            if rule_track == track.id and rule_type not in type_ids:
                kind = session.get(ActivityType, rule_type)
                if kind is not None:
                    type_ids[kind.id] = kind.name
        for type_id, type_name in type_ids.items():
            user_id = rules.get((track.id, type_id))
            rows.append({"key": f"{track.id}:{type_id}", "Track": track.name, "Task": type_name,
                         "Goes to": users.get(user_id, LEAD_OWNER) if user_id else LEAD_OWNER})
    return pd.DataFrame(rows).set_index("key") if rows else pd.DataFrame()


def save_assignments(session: Session, edited: pd.DataFrame, user: User) -> int:
    _require_admin(user)
    by_name = {u.name: u.id for u in session.scalars(select(User).where(User.is_active.is_(True)))}
    existing = {(a.track_id, a.activity_type_id): a for a in session.scalars(select(TaskAssignment))}
    changed = 0
    for key, row in edited.iterrows():
        track_id, type_id = (int(x) for x in str(key).split(":"))
        target = by_name.get(row["Goes to"]) if row["Goes to"] not in (None, LEAD_OWNER) else None
        rule = existing.get((track_id, type_id))
        before = rule.user_id if rule else None
        if before == target:
            continue
        if rule is None:
            session.add(TaskAssignment(track_id=track_id, activity_type_id=type_id, user_id=target))
        elif target is None:
            session.delete(rule)
        else:
            rule.user_id = target
        session.add(AuditLog(user_id=user.id, entity="task_assignments", entity_id=str(key), action="update",
                             diff={f"{row['Track']} · {row['Task']}": [before, target]}))
        changed += 1
    session.flush()
    return changed
