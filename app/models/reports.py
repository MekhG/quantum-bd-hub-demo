"""Weekly report (Week 8): weekly targets, each person's notes for the week, and the saved reports.

Weeks run Monday to Sunday and are identified by their Monday (week_start).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TZDateTime, TimestampMixin

REPORT_STATUSES = ("draft", "final")


class WeeklyTarget(Base):
    """One person's target for one metric in one week, e.g. Kabir · Outreach · 120 (set by the admin on Mondays)."""

    __tablename__ = "weekly_targets"
    __table_args__ = (UniqueConstraint("week_start", "user_id", "metric", name="uq_weekly_targets_week_user_metric"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Keys from app.services.report_metrics.TARGET_METRICS, e.g. 'outreach', 'followup_calls'.
    metric: Mapped[str] = mapped_column(String(40))
    target: Mapped[int] = mapped_column(Integer)


class ReportNote(TimestampMixin, Base):
    """What a person wants in this week's report: highlights, blockers, asks. Written on Today."""

    __tablename__ = "report_notes"
    __table_args__ = (UniqueConstraint("week_start", "user_id", name="uq_report_notes_week_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str | None] = mapped_column(Text)


class WeeklyReport(TimestampMixin, Base):
    """The report for one week. Final reports keep a frozen copy of their numbers (snapshot), so what leadership
    saw never changes when activities are edited later."""

    __tablename__ = "weekly_reports"
    __table_args__ = (CheckConstraint(f"status IN {REPORT_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True)
    status: Mapped[str] = mapped_column(String(10), default="draft", server_default="draft")
    highlights: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[str | None] = mapped_column(Text)
    asks: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict | None] = mapped_column(JSONType)
    finalised_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    finalised_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
