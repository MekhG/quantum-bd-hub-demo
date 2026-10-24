"""Configuration tables: the things that change as the BD process changes.

Tracks, stages, cadences, activity types, products, lost reasons and the small
dropdown lists (tier, potential, contact type, department…) live here as data,
so they can be edited without touching code.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin

STAGE_OUTCOMES = ("open", "won", "lost", "nurture")
PRODUCT_CATEGORIES = ("simulator", "kit")


class Track(TimestampMixin, Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # stable key for imports/seeds
    goal: Mapped[str | None] = mapped_column(Text)
    sender_mailbox: Mapped[str | None] = mapped_column(String(255))
    # e.g. Schools: ["Senior secondary", "Secondary"]; leads in this track pick one.
    sub_tiers: Mapped[list[str] | None] = mapped_column(JSONType)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    notes: Mapped[str | None] = mapped_column(Text)
    # Start the cadence by itself as soon as a lead in this track has a verified contact.
    auto_start_cadence: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Optional limit on new cadence starts per day (protects the sender mailbox). None = no limit.
    daily_start_cap: Mapped[int | None] = mapped_column(Integer)

    cadence_steps: Mapped[list[CadenceStep]] = relationship(
        back_populates="track", order_by="CadenceStep.step_no", cascade="all, delete-orphan"
    )


class Stage(Base):
    __tablename__ = "stages"
    __table_args__ = (
        CheckConstraint(f"outcome IN {STAGE_OUTCOMES}", name="outcome_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Reporting groups stages by outcome, so renaming a stage never breaks the dashboard.
    outcome: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Other wordings the importer accepts, e.g. 'Lost' or 'Not interested' for Rejected.
    aliases: Mapped[list[str] | None] = mapped_column(JSONType)


class ActivityType(Base):
    __tablename__ = "activity_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    # Groups types for filters and icons: email, phone, whatsapp, meeting, demo, linkedin, internal, task.
    channel: Mapped[str] = mapped_column(String(20))
    default_duration_min: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    # Internal discussions and tasks are logged but don't count as touching the lead.
    counts_as_touch: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


cadence_step_activity_types = Table(
    "cadence_step_activity_types",
    Base.metadata,
    Column("cadence_step_id", ForeignKey("cadence_steps.id", ondelete="CASCADE"), primary_key=True),
    Column("activity_type_id", ForeignKey("activity_types.id", ondelete="RESTRICT"), primary_key=True),
)


class CadenceStep(Base):
    __tablename__ = "cadence_steps"
    __table_args__ = (UniqueConstraint("track_id", "step_no", name="uq_cadence_steps_track_step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    step_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80))
    # Planned date = previous step done + days_after_previous; overdue after grace_days more.
    # "4 days later, overdue after 2 more" is stored as days_after_previous=4, grace_days=2.
    days_after_previous: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    grace_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # True for steps like "call OR email": doing either one completes the step.
    complete_on_any: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    notes: Mapped[str | None] = mapped_column(Text)

    track: Mapped[Track] = relationship(back_populates="cadence_steps")
    activity_types: Mapped[list[ActivityType]] = relationship(secondary=cadence_step_activity_types)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(f"category IN {PRODUCT_CATEGORIES}", name="category_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[str] = mapped_column(String(20))
    is_mini: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    description: Mapped[str | None] = mapped_column(Text)
    # Days from purchase order to delivery: software ~15, hardware ~90.
    delivery_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Other ways the product is written in sheets, e.g. ["qkd kit", "eps qkd"], so imports recognise it.
    aliases: Mapped[list[str] | None] = mapped_column(JSONType)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class LostReason(Base):
    __tablename__ = "lost_reasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class OptionList(Base):
    """Small editable dropdowns that don't deserve a table each.

    list_name groups them: 'tier', 'potential', 'contact_type', 'department',
    'lead_channel', 'response_source'. aliases lets imports recognise variants
    such as 'Dept. of Physics' → 'Physics'.
    """

    __tablename__ = "option_lists"
    __table_args__ = (UniqueConstraint("list_name", "value", name="uq_option_lists_list_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_name: Mapped[str] = mapped_column(String(40), index=True)
    value: Mapped[str] = mapped_column(String(80))
    aliases: Mapped[list[str] | None] = mapped_column(JSONType)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class TaskAssignment(Base):
    """Who gets the planned tasks of each type in each track, e.g. Colleges · Email → Kabir.

    Set by the admin on the Configuration page. No row = the lead's owner, else the track's fallback.
    """

    __tablename__ = "task_assignments"
    __table_args__ = (UniqueConstraint("track_id", "activity_type_id", name="uq_task_assignments_track_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    activity_type_id: Mapped[int] = mapped_column(ForeignKey("activity_types.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
