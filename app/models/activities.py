"""Unified activity model: every call, email, WhatsApp, meeting, demo and follow-up.

One table holds both what happened and what is planned, so the lead timeline,
the Today view and the team calendar all read from the same place.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TZDateTime, TimestampMixin
from app.models.config_tables import ActivityType, CadenceStep
from app.models.core import Contact, Lead, Organisation, User

ACTIVITY_STATUSES = ("planned", "done", "cancelled", "missed")
ACTIVITY_DIRECTIONS = ("outbound", "inbound", "internal")


class Activity(TimestampMixin, Base):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(f"status IN {ACTIVITY_STATUSES}", name="status_valid"),
        CheckConstraint(f"direction IN {ACTIVITY_DIRECTIONS}", name="direction_valid"),
        # A planned activity with no date would never show up in Today or the calendar.
        CheckConstraint("status <> 'planned' OR scheduled_at IS NOT NULL", name="planned_has_date"),
        Index("ix_activities_owner_status_scheduled", "owner_user_id", "status", "scheduled_at"),
        Index("ix_activities_lead_scheduled", "lead_id", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type_id: Mapped[int] = mapped_column(ForeignKey("activity_types.id", ondelete="RESTRICT"))
    # History must survive a lead being removed, so links are cleared rather than cascading.
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="planned", server_default="planned")
    scheduled_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    occurred_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    duration_min: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(10), default="outbound", server_default="outbound")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the cadence engine created this activity.
    cadence_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("cadence_steps.id", ondelete="SET NULL")
    )
    # Chains each follow-up to the activity whose "what's next?" prompt created it.
    follow_up_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL")
    )
    outcome: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The script used for this email/call, from the script library (Week 5).
    script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id", ondelete="SET NULL"))

    activity_type: Mapped[ActivityType] = relationship()
    lead: Mapped[Lead | None] = relationship()
    organisation: Mapped[Organisation | None] = relationship()
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_user_id])
    cadence_step: Mapped[CadenceStep | None] = relationship()
    participants: Mapped[list[ActivityParticipant]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )


class ActivityParticipant(Base):
    """Who took part: university contacts and/or team members. One row per person."""

    __tablename__ = "activity_participants"
    __table_args__ = (
        CheckConstraint(
            "(contact_id IS NULL) <> (user_id IS NULL)", name="exactly_one_person"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    # Cascades so a DPDP deletion request removes the person cleanly from every activity.
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str | None] = mapped_column(String(60))  # e.g. 'HoD', 'decision maker', 'presenter'

    activity: Mapped[Activity] = relationship(back_populates="participants")
    contact: Mapped[Contact | None] = relationship()
    user: Mapped[User | None] = relationship()
