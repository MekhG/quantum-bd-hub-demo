"""Procurement & quotes (Week 6): price list, bundles, quotations, procurement checklists, app settings.

Quotes keep a copy of every price and description they were made with (quote_lines), so changing the price list
never changes a quote that was already sent, and its PDF can be regenerated identically at any time.
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
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.config_tables import Product
from app.models.core import Contact, Opportunity, User

QUOTE_STATUSES = ("draft", "sent", "accepted", "expired", "superseded", "cancelled")
PRICE_BASES = ("institute", "partner")
CHECKLIST_STATUSES = ("to_do", "requested", "received", "submitted", "not_needed")


class PriceItem(TimestampMixin, Base):
    """One sellable line: a product, a variant of it, or a service (installation, training, AMC).
    name is the bold line on the quotation; description is the paragraph under it."""

    __tablename__ = "price_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(40), default="unit", server_default="unit")
    partner_price_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    institute_price_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    gst_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("18"), server_default="18")
    hsn_sac: Mapped[str | None] = mapped_column(String(20))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    product: Mapped[Product | None] = relationship()


class Bundle(Base):
    """A package, e.g. 'Starter simulation lab': a named set of price items."""

    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    phase: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    items: Mapped[list[BundleItem]] = relationship(back_populates="bundle", cascade="all, delete-orphan")


class BundleItem(Base):
    __tablename__ = "bundle_items"

    bundle_id: Mapped[int] = mapped_column(ForeignKey("bundles.id", ondelete="CASCADE"), primary_key=True)
    price_item_id: Mapped[int] = mapped_column(ForeignKey("price_items.id", ondelete="CASCADE"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    bundle: Mapped[Bundle] = relationship(back_populates="items")
    price_item: Mapped[PriceItem] = relationship()


class Quote(TimestampMixin, Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint(f"status IN {QUOTE_STATUSES}", name="status_valid"),
        CheckConstraint(f"price_basis IN {PRICE_BASES}", name="basis_valid"),
        UniqueConstraint("number", "version", name="uq_quotes_number_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(40))          # e.g. QA/2026-27/014 — shared by all versions
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(12), default="draft", server_default="draft")
    price_basis: Mapped[str] = mapped_column(String(10), default="institute", server_default="institute")
    quote_date: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date] = mapped_column(Date)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), server_default="0")
    subtotal_inr: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    discount_inr: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    gst_inr: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    total_inr: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    # The recipient as written on the quote (kept even if the contact changes later).
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    to_name: Mapped[str | None] = mapped_column(String(160))
    to_designation: Mapped[str | None] = mapped_column(String(160))
    to_institution: Mapped[str | None] = mapped_column(String(255))
    # Cover page of the Word quotation (the template's {title}, {attention}, {greeting}, {prepared_by}).
    title: Mapped[str | None] = mapped_column(String(200))
    attention: Mapped[str | None] = mapped_column(String(200))
    greeting: Mapped[str | None] = mapped_column(String(200))
    prepared_by: Mapped[str | None] = mapped_column(String(200))
    terms: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    drive_url: Mapped[str | None] = mapped_column(String(1000))   # optional copy of the PDF in Google Drive
    sent_on: Mapped[date | None] = mapped_column(Date)
    accepted_on: Mapped[date | None] = mapped_column(Date)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    opportunity: Mapped[Opportunity] = relationship()
    contact: Mapped[Contact | None] = relationship()
    created_by: Mapped[User | None] = relationship()
    lines: Mapped[list[QuoteLine]] = relationship(back_populates="quote", cascade="all, delete-orphan",
                                                  order_by="QuoteLine.sort_order")


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"), index=True)
    price_item_id: Mapped[int | None] = mapped_column(ForeignKey("price_items.id", ondelete="SET NULL"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    description: Mapped[str] = mapped_column(String(400))   # the bold item name
    details: Mapped[str | None] = mapped_column(Text)        # the paragraph under it
    section: Mapped[str] = mapped_column(String(20), default="other", server_default="other")  # software/hardware/other
    unit: Mapped[str | None] = mapped_column(String(40))
    hsn_sac: Mapped[str | None] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    unit_price_inr: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gst_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("18"), server_default="18")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    quote: Mapped[Quote] = relationship(back_populates="lines")


class ProcurementRule(Base):
    """Which documents each buying route needs (config): e.g. PAC → proprietary certificate from the OEM."""

    __tablename__ = "procurement_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    buying_route: Mapped[str] = mapped_column(String(80), index=True)
    item: Mapped[str] = mapped_column(String(200))
    only_if_reseller: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ProcurementItem(TimestampMixin, Base):
    """One document on a deal's checklist, with where it stands."""

    __tablename__ = "procurement_items"
    __table_args__ = (CheckConstraint(f"status IN {CHECKLIST_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    item: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="to_do", server_default="to_do")
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    due_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AppSetting(Base):
    """Small key/value settings edited on the Configuration page (quotation letterhead details, validity…)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
