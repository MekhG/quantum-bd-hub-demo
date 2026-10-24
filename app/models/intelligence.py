"""News & signals (Week 7): news searches, the items they bring in, signal types and the signals saved against
institutions.

A signal is a dated fact about an institution that makes a sale more likely: an NQM lab approved, a tender, a new
centre of excellence. Each signal type carries points that fade over its decay period; the points add up to the
lead score. Automatic signal types (code set) are worked out by the app from the hub's own data: an open quote,
a recent reply, a demo, the NQM flag.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TZDateTime, TimestampMixin
from app.models.core import Organisation, User

NEWS_KINDS = ("google_news", "rss")
NEWS_STATUSES = ("new", "saved", "signal", "dismissed")
# Automatic signal types: the app computes these from the hub's data, so people can change their points but
# can't add them by hand.
AUTO_SIGNAL_CODES = ("open_quote", "demo_held", "replied_recently", "nqm_applicant")


class SignalType(Base):
    __tablename__ = "signal_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # Set only for automatic types (see AUTO_SIGNAL_CODES).
    code: Mapped[str | None] = mapped_column(String(40), unique=True)
    weight: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    # Points fade in a straight line to zero over this many months (a 6-month-old 12-month signal counts half).
    decay_months: Mapped[int] = mapped_column(Integer, default=12, server_default="12")
    # Headline words that suggest this signal type for a news item, e.g. ["tender", "rfq", "bid"].
    keywords: Mapped[list[str] | None] = mapped_column(JSONType)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class NewsSource(TimestampMixin, Base):
    """One news search (Google News) or feed (RSS). Free feeds only — no paid API, no AI."""

    __tablename__ = "news_sources"
    __table_args__ = (CheckConstraint(f"kind IN {NEWS_KINDS}", name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="google_news", server_default="google_news")
    # Google News search words, e.g. "National Quantum Mission" (quotes = exact phrase, OR between words).
    query: Mapped[str | None] = mapped_column(String(300))
    # RSS address (kind = rss).
    url: Mapped[str | None] = mapped_column(String(1000))
    topic: Mapped[str] = mapped_column(String(80))
    # Keep an item only if it mentions one of these comma-separated words (e.g. "quantum" for the PIB feed,
    # which carries every ministry's releases).
    must_include: Mapped[str | None] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_fetched_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    last_count: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        CheckConstraint(f"status IN {NEWS_STATUSES}", name="status_valid"),
        Index("ix_news_items_status_published", "status", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    summary: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("news_sources.id", ondelete="SET NULL"))
    topic: Mapped[str | None] = mapped_column(String(80))
    suggested_type_id: Mapped[int | None] = mapped_column(ForeignKey("signal_types.id", ondelete="SET NULL"))
    # Institutions with a lead whose name (or alias) appears in the headline or summary.
    matched_org_ids: Mapped[list[int] | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(12), default="new", server_default="new")
    library_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    fetched_at: Mapped[datetime] = mapped_column(TZDateTime(), server_default=func.now())
    actioned_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actioned_at: Mapped[datetime | None] = mapped_column(TZDateTime())

    source: Mapped[NewsSource | None] = relationship()
    suggested_type: Mapped[SignalType | None] = relationship()


class OrgSignal(TimestampMixin, Base):
    """A signal saved against an institution — it counts for every lead the institution has."""

    __tablename__ = "org_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    signal_type_id: Mapped[int] = mapped_column(ForeignKey("signal_types.id", ondelete="RESTRICT"))
    observed_on: Mapped[date] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    news_item_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    organisation: Mapped[Organisation] = relationship()
    signal_type: Mapped[SignalType] = relationship()
    created_by: Mapped[User | None] = relationship()
