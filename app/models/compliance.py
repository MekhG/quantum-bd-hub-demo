"""Compliance tables for the DPDP Act: where contacts came from, opt-outs, rights requests, audit trail."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TZDateTime
from app.models.core import Contact

CONSENT_BASES = ("consent", "legitimate_use", "publicly_available", "other")
DATA_REQUEST_TYPES = ("access", "correction", "deletion", "withdrawal", "grievance")
AUDIT_ACTIONS = ("create", "update", "delete", "import", "export")


class ConsentLog(Base):
    """Evidence of where a contact's details came from and what they agreed to, and when."""

    __tablename__ = "consent_log"
    __table_args__ = (CheckConstraint(f"basis IN {CONSENT_BASES}", name="basis_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(160))  # e.g. 'University website', 'Event: QIndia 2026'
    basis: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str | None] = mapped_column(String(255))
    captured_on: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    captured_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    withdrawn_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    contact: Mapped[Contact] = relationship()


class Suppression(Base):
    """Global do-not-contact list across all tracks and mailboxes.

    Stores a SHA-256 hash of the email rather than the email itself, so an opted-out
    person stays blocked on future imports even after their contact record is deleted —
    without us keeping their address.
    """

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    reason: Mapped[str | None] = mapped_column(String(160))  # 'unsubscribed', 'bounced', 'asked by phone'
    added_at: Mapped[datetime] = mapped_column(TZDateTime(), server_default=func.now())
    added_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DataRequest(Base):
    """DPDP data-principal requests (access, correction, deletion…) and how they were closed."""

    __tablename__ = "data_requests"
    __table_args__ = (
        CheckConstraint(f"request_type IN {DATA_REQUEST_TYPES}", name="request_type_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cleared, not cascaded: the request log must outlive the contact it deleted.
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    requester_name: Mapped[str | None] = mapped_column(String(160))
    request_type: Mapped[str] = mapped_column(String(20))
    received_on: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    due_on: Mapped[date | None] = mapped_column(Date)
    closed_on: Mapped[date | None] = mapped_column(Date)
    handled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    # Added in the privacy-requests screen (2026-10-21).
    requester_email: Mapped[str | None] = mapped_column(String(255))
    channel: Mapped[str | None] = mapped_column(String(40))          # email, phone, letter, WhatsApp, in person
    status: Mapped[str] = mapped_column(String(12), default="open", server_default="open")
    community_member_id: Mapped[int | None] = mapped_column(ForeignKey("community_members.id",
                                                                       ondelete="SET NULL"))
    action_taken: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZDateTime(), server_default=func.now())


DATA_REQUEST_STATUSES = ("open", "in_progress", "done", "rejected")


class AuditLog(Base):
    """Who changed what, and when."""

    __tablename__ = "audit_log"
    __table_args__ = (CheckConstraint(f"action IN {AUDIT_ACTIONS}", name="action_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    entity: Mapped[str] = mapped_column(String(60))  # table name, e.g. 'contacts'
    entity_id: Mapped[str | None] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(10))
    changed_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now(), index=True
    )
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
