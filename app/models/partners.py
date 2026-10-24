"""Partners & deal registration, and the installed base (post-sale).

Partners: resellers, distribution, referral, training and technology partners. A partner registers a deal for an
institution; once approved it is locked to that partner for a while (default 60 days) so two partners — or a
partner and Demo — don't chase the same institution.

Installed base: one record per won deal, from delivery to installation, acceptance, product training, warranty,
AMC, the next purchase and upsell.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin
from app.models.core import Contact, Lead, Opportunity, Organisation, User

PARTNER_STATUSES = ("prospect", "active", "paused", "ended")
REGISTRATION_STATUSES = ("pending", "approved", "rejected", "expired", "won", "lost")
AMC_STATUSES = ("none", "quoted", "active", "lapsed")


class Partner(TimestampMixin, Base):
    __tablename__ = "partners"
    __table_args__ = (CheckConstraint(f"status IN {PARTNER_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), unique=True)
    # Reseller, Distribution partner, Referral partner, Training partner… (option list 'partner_model').
    model: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    nda_signed_on: Mapped[date | None] = mapped_column(Date)
    non_compete: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    nda_doc_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    terms_doc_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    price_list_version: Mapped[str | None] = mapped_column(String(60))
    payment_terms: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    territory: Mapped[str | None] = mapped_column(String(200))     # e.g. "Telangana, Andhra Pradesh"
    notes: Mapped[str | None] = mapped_column(Text)

    organisation: Mapped[Organisation] = relationship()
    contact: Mapped[Contact | None] = relationship()
    owner: Mapped[User | None] = relationship()


class DealRegistration(TimestampMixin, Base):
    __tablename__ = "deal_registrations"
    __table_args__ = (CheckConstraint(f"status IN {REGISTRATION_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    product_ids: Mapped[list[int] | None] = mapped_column(JSONType)
    expected_value_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    contact_name: Mapped[str | None] = mapped_column(String(160))   # the institution's person the partner deals with
    registered_on: Mapped[date] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)            # set on approval
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    # What the overlap check found when the deal was registered (shown to the approver).
    conflict_note: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_on: Mapped[date | None] = mapped_column(Date)
    decision_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    partner: Mapped[Partner] = relationship()
    organisation: Mapped[Organisation] = relationship()
    lead: Mapped[Lead | None] = relationship()


class Installation(TimestampMixin, Base):
    """The installed base: what happened after a deal was won."""

    __tablename__ = "installations"
    __table_args__ = (CheckConstraint(f"amc_status IN {AMC_STATUSES}", name="amc_status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True)
    delivered_on: Mapped[date | None] = mapped_column(Date)
    installed_on: Mapped[date | None] = mapped_column(Date)
    accepted_on: Mapped[date | None] = mapped_column(Date)           # acceptance starts the warranty
    training_on: Mapped[date | None] = mapped_column(Date)           # Demo product training
    training_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    warranty_months: Mapped[int] = mapped_column(Integer, default=12, server_default="12")
    warranty_end: Mapped[date | None] = mapped_column(Date)
    amc_status: Mapped[str] = mapped_column(String(10), default="none", server_default="none")
    amc_start: Mapped[date | None] = mapped_column(Date)
    amc_end: Mapped[date | None] = mapped_column(Date)
    amc_value_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    next_phase: Mapped[str | None] = mapped_column(String(200))     # e.g. "Phase 2 — experimentation kits"
    upsell_on: Mapped[date | None] = mapped_column(Date)
    reference_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    case_study_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    notes: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship()
