"""Knowledge base (Week 5): documents (as links), scripts with fill-in fields, and the objection library.

Documents are links (Google Drive, website, video) — the hub stores what each one is and where it lives, not the
file itself. Each document and script keeps its versions: rows share a family_id, and exactly one row per family
is current.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.config_tables import Product, Track
from app.models.core import User

LIBRARY_STATUSES = ("draft", "approved", "archived")
SCRIPT_KINDS = ("email", "call", "whatsapp", "linkedin")

document_products = Table(
    "document_products",
    Base.metadata,
    Column("document_id", ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)
document_tracks = Table(
    "document_tracks",
    Base.metadata,
    Column("document_id", ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
    Column("track_id", ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
)
objection_products = Table(
    "objection_products",
    Base.metadata,
    Column("objection_id", ForeignKey("objections.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)
objection_tracks = Table(
    "objection_tracks",
    Base.metadata,
    Column("objection_id", ForeignKey("objections.id", ondelete="CASCADE"), primary_key=True),
    Column("track_id", ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
)
# Which documents were shared in an activity ("sent the QSim Cluster brochure to X").
activity_documents = Table(
    "activity_documents",
    Base.metadata,
    Column("activity_id", ForeignKey("activities.id", ondelete="CASCADE"), primary_key=True),
    Column("document_id", ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
)


class _Versioned:
    family_id: Mapped[int | None] = mapped_column(Integer, index=True)  # id of the first version
    version_no: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    status: Mapped[str] = mapped_column(String(10), default="draft", server_default="draft")
    approved_on: Mapped[date | None] = mapped_column(Date)


class Document(_Versioned, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (CheckConstraint(f"status IN {LIBRARY_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    doc_type: Mapped[str] = mapped_column(String(60))      # from the 'document_type' option list
    url: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    # Pasted main points of the file, so search can find what's inside a link.
    key_text: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(400))  # comma-separated free tags
    version_label: Mapped[str | None] = mapped_column(String(40))  # e.g. "Oct 2026" or "v3"
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    products: Mapped[list[Product]] = relationship(secondary=document_products)
    tracks: Mapped[list[Track]] = relationship(secondary=document_tracks)
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_user_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_user_id])


class Script(_Versioned, TimestampMixin, Base):
    """An email / call / WhatsApp script. Fill-in fields like {first_name} are replaced per contact."""

    __tablename__ = "scripts"
    __table_args__ = (
        CheckConstraint(f"status IN {LIBRARY_STATUSES}", name="status_valid"),
        CheckConstraint(f"kind IN {SCRIPT_KINDS}", name="kind_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="email", server_default="email")
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    # Tie to a cadence step so the email batch knows which script each row uses.
    cadence_step_id: Mapped[int | None] = mapped_column(ForeignKey("cadence_steps.id", ondelete="SET NULL"))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    track: Mapped[Track | None] = relationship()
    owner: Mapped[User | None] = relationship(foreign_keys=[owner_user_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_user_id])


class Objection(TimestampMixin, Base):
    """'They say' → 'we say': short answers to common objections, tagged by product and track."""

    __tablename__ = "objections"
    __table_args__ = (CheckConstraint(f"status IN {LIBRARY_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    objection: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(10), default="draft", server_default="draft")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    times_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    products: Mapped[list[Product]] = relationship(secondary=objection_products)
    tracks: Mapped[list[Track]] = relationship(secondary=objection_tracks)
    owner: Mapped[User | None] = relationship()
