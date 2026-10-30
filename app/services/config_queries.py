"""Read the configuration tables as DataFrames, and export them to Excel."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    ActivityType,
    CadenceStep,
    Contact,
    Lead,
    LostReason,
    OptionList,
    Organisation,
    Product,
    Stage,
    Track,
    User,
)


def database_version(session: Session) -> str:
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return "SQLite " + str(session.execute(text("select sqlite_version()")).scalar_one()) + " (demo database)"
    return str(session.execute(text("select version()")).scalar_one())


def table_counts(session: Session) -> dict[str, int]:
    models = {
        "Tracks": Track,
        "Cadence steps": CadenceStep,
        "Stages": Stage,
        "Activity types": ActivityType,
        "Products": Product,
        "Team members": User,
        "Organisations": Organisation,
        "Contacts": Contact,
        "Leads": Lead,
        "Activities": Activity,
    }
    return {
        label: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for label, model in models.items()
    }


def tracks_frame(session: Session) -> pd.DataFrame:
    tracks = session.scalars(select(Track).order_by(Track.sort_order))
    return pd.DataFrame(
        [
            {
                "Track": t.name,
                "Code": t.code,
                "Goal": t.goal or "",
                "Sender mailbox": t.sender_mailbox or "— to fill in —",
                "Sub-tiers": ", ".join(t.sub_tiers or []),
                "Active": t.is_active,
                "Notes": t.notes or "",
            }
            for t in tracks
        ]
    )


def cadence_frame(session: Session) -> pd.DataFrame:
    steps = session.scalars(
        select(CadenceStep)
        .join(Track)
        .options(selectinload(CadenceStep.activity_types), selectinload(CadenceStep.track))
        .order_by(Track.sort_order, CadenceStep.step_no)
    )
    rows = []
    for s in steps:
        joiner = " or " if s.complete_on_any else " + "
        window = (
            f"{s.days_after_previous}–{s.days_after_previous + s.grace_days} days"
            if s.grace_days
            else f"{s.days_after_previous} days"
        )
        rows.append(
            {
                "Track": s.track.name,
                "Step": s.step_no,
                "Name": s.name,
                "Activities": joiner.join(t.name for t in s.activity_types),
                "After previous step": "start" if s.step_no == 1 else window,
            }
        )
    return pd.DataFrame(rows)


def stages_frame(session: Session) -> pd.DataFrame:
    stages = session.scalars(select(Stage).order_by(Stage.sort_order))
    return pd.DataFrame(
        [{"Order": s.sort_order, "Stage": s.name, "Outcome": s.outcome, "Active": s.is_active} for s in stages]
    )


def activity_types_frame(session: Session) -> pd.DataFrame:
    types = session.scalars(select(ActivityType).order_by(ActivityType.sort_order))
    return pd.DataFrame(
        [
            {
                "Activity type": t.name,
                "Channel": t.channel,
                "Default minutes": t.default_duration_min,
                "Counts as a touch": t.counts_as_touch,
                "Active": t.is_active,
            }
            for t in types
        ]
    )


def products_frame(session: Session) -> pd.DataFrame:
    products = session.scalars(select(Product).order_by(Product.sort_order))
    return pd.DataFrame(
        [
            {
                "Product": p.name,
                "Category": p.category,
                "Mini": p.is_mini,
                "Delivery days after PO": p.delivery_days,
                "Also recognised as": ", ".join(p.aliases or []),
                "Description": p.description or "",
            }
            for p in products
        ]
    )


def lost_reasons_frame(session: Session) -> pd.DataFrame:
    reasons = session.scalars(select(LostReason).order_by(LostReason.sort_order))
    return pd.DataFrame([{"Lost reason": r.name, "Active": r.is_active} for r in reasons])


LIST_LABELS = {
    "industry": "Industry",
    "tier": "Tier",
    "potential": "Potential",
    "contact_type": "Contact type",
    "department": "Department",
    "lead_channel": "Lead channel",
    "response_source": "Response source",
}


def option_lists_frame(session: Session) -> pd.DataFrame:
    options = session.scalars(select(OptionList).order_by(OptionList.list_name, OptionList.sort_order))
    return pd.DataFrame(
        [
            {
                "List": LIST_LABELS.get(o.list_name, o.list_name),
                "Value": o.value,
                "Also recognised as": ", ".join(o.aliases or []),
                "Active": o.is_active,
            }
            for o in options
        ]
    )


def team_frame(session: Session) -> pd.DataFrame:
    users = session.scalars(select(User).order_by(User.id))
    return pd.DataFrame(
        [{"Name": u.name, "Sign-in email": u.email, "Role": u.role, "Active": u.is_active} for u in users]
    )


def all_config_frames(session: Session) -> dict[str, pd.DataFrame]:
    return {
        "Tracks": tracks_frame(session),
        "Cadences": cadence_frame(session),
        "Stages": stages_frame(session),
        "Activity types": activity_types_frame(session),
        "Products": products_frame(session),
        "Lost reasons": lost_reasons_frame(session),
        "Dropdown lists": option_lists_frame(session),
        "Team": team_frame(session),
        "Signal types": _signal_types(session),
        "News sources": _news_sources(session),
    }


def _signal_types(session: Session) -> pd.DataFrame:
    from app.services.signals import types_frame

    return types_frame(session).drop(columns=["id"])


def _news_sources(session: Session) -> pd.DataFrame:
    from app.services.news import sources_frame

    return sources_frame(session).drop(columns=["id"])


def frames_to_xlsx(frames: dict[str, pd.DataFrame]) -> bytes:
    """One sheet per table; Excel limits sheet names to 31 characters."""
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in frames.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    return buffer.getvalue()
