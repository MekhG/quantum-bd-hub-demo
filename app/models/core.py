"""Core CRM tables: people on the team, organisations, contacts, leads, opportunities."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TZDateTime, TimestampMixin
from app.models.config_tables import LostReason, Product, Stage, Track

USER_ROLES = ("admin", "member", "viewer")
ORG_TYPES = (
    "company",
    "startup",
    "university",
    "college",
    "standalone",  # AISHE's word for an institute outside the university system
    "school",
    "research_institute",
    "government",
    "non_profit",
    "partner",
    "individual",  # a person with no organisation (e.g. some Mini-product buyers)
    "other",
)
# Types that make up the academic prospect pool (the AISHE-style college list).
ACADEMIC_ORG_TYPES = ("college", "standalone", "university")
OPPORTUNITY_STATUSES = ("open", "won", "lost")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IN {USER_ROLES}", name="role_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(10), default="member", server_default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Organisation(TimestampMixin, Base):
    __tablename__ = "organisations"
    __table_args__ = (CheckConstraint(f"org_type IN {ORG_TYPES}", name="org_type_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    org_type: Mapped[str] = mapped_column(String(20), default="company", server_default="company")
    # Sector the organisation works in, e.g. 'Higher education', 'Telecom' (values come from option_lists, so the
    # admin can add industries without a code change). Filters Organisations, Contacts and Leads.
    industry: Mapped[str | None] = mapped_column(String(80), index=True)
    state: Mapped[str | None] = mapped_column(String(60))
    city: Mapped[str | None] = mapped_column(String(80))
    # ---- education details (only filled for universities and colleges) ----
    # AISHE code: the government's ID for every Indian higher-education institution — the best duplicate key.
    aishe_code: Mapped[str | None] = mapped_column(String(20), unique=True)
    affiliated_university: Mapped[str | None] = mapped_column(String(255))
    # Institution quality: 'Tier 1' / 'Tier 2' / 'Tier 3' (values come from option_lists).
    tier: Mapped[str | None] = mapped_column(String(20))
    # 'Rural' / 'Urban' from the AISHE list.
    area: Mapped[str | None] = mapped_column(String(20))
    # Who runs it: 'State Government', 'Central Government', 'Private Un-Aided'… Government
    # institutions mostly buy through GeM, so this matters for the procurement route later.
    management: Mapped[str | None] = mapped_column(String(80))
    # Finer type from AISHE: 'Affiliated College', 'Technical/Polytechnic', 'Teacher Training'…
    category: Mapped[str | None] = mapped_column(String(80))
    # Which raw lists the institution appeared in, comma-separated (e.g. 'Vidyalaxmi, Rural list').
    source_lists: Mapped[str | None] = mapped_column(Text)
    # ---- every organisation ----
    website: Mapped[str | None] = mapped_column(String(255))
    # Email/web domain (e.g. 'iitb.ac.in') is the next-best duplicate key after AISHE code.
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    aicte_affiliated: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    # Other names the news uses, e.g. ["IIT Bombay", "IITB"] — used to spot the institution in headlines.
    aliases: Mapped[list[str] | None] = mapped_column(JSONType)

    contacts: Mapped[list[Contact]] = relationship(back_populates="organisation")
    leads: Mapped[list[Lead]] = relationship(back_populates="organisation")


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    designation: Mapped[str | None] = mapped_column(String(160))
    # Faculty, HoD, Dean, Director/Principal, Registrar, Purchase/Admin, Other (option_lists).
    contact_type: Mapped[str | None] = mapped_column(String(60))
    department: Mapped[str | None] = mapped_column(String(160))
    research_interests: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    linkedin_url: Mapped[str | None] = mapped_column(String(255))
    # 'bounced' once an email to this address came back undeliverable; such contacts never get cadence emails.
    email_status: Mapped[str | None] = mapped_column(String(20))

    # Only verified contacts may enter an email cadence (deliverability guardrail).
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    verified_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    verified_on: Mapped[date | None] = mapped_column(Date)
    verification_note: Mapped[str | None] = mapped_column(String(255))

    # Where this contact came from — needed for the DPDP consent/source record.
    source: Mapped[str | None] = mapped_column(String(120))

    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    do_not_contact_reason: Mapped[str | None] = mapped_column(String(255))
    do_not_contact_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    organisation: Mapped[Organisation | None] = relationship(back_populates="contacts")


# Which products look like a fit for a lead (the tracker's "Product Fit" column).
lead_products = Table(
    "lead_products",
    Base.metadata,
    Column("lead_id", ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
)


class Lead(TimestampMixin, Base):
    """An institution being worked in one track. One institution can sit in several tracks."""

    __tablename__ = "leads"
    __table_args__ = (
        # Duplicate guard: the same organisation can't be added twice to the same track.
        UniqueConstraint("org_id", "track_id", name="uq_leads_org_track"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="RESTRICT"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="RESTRICT"), index=True)
    # The person currently engaging for this institution; switchable as the conversation moves.
    primary_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id", ondelete="RESTRICT"), index=True)
    sub_tier: Mapped[str | None] = mapped_column(String(60))
    # Likelihood of closing: '0%', '30-50%', '50-70%', '70-90%', '100%' (option_lists).
    potential: Mapped[str | None] = mapped_column(String(20))
    # Interests from the sheets that aren't products, e.g. "Quantum Computing Curriculum; Quantum Awareness".
    interest_areas: Mapped[str | None] = mapped_column(Text)
    # Who brought or works the lead: Direct, Reseller, Referral, Other (option_lists).
    channel: Mapped[str] = mapped_column(String(40), default="Direct", server_default="Direct")
    nqm_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # e.g. 'Referral', 'Fresh', 'Inbound – website', 'Event'.
    source: Mapped[str | None] = mapped_column(String(120))
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Which cadence step the lead is on; None = not in a cadence yet.
    current_step_no: Mapped[int | None] = mapped_column(Integer)
    # Cadence state: None (never started) · scheduled (starts on a later day because of the daily cap) ·
    # active · awaiting_reply (all steps done, waiting before suggesting Ghosted) · finished (suggest Ghosted)
    # · replied · stopped · ghosted · kept_open.
    cadence_status: Mapped[str | None] = mapped_column(String(20), index=True)
    cadence_started_on: Mapped[date | None] = mapped_column(Date)
    # Next date the cadence needs attention (step due / day to suggest Ghosted).
    cadence_due_on: Mapped[date | None] = mapped_column(Date)
    cadence_ended_on: Mapped[date | None] = mapped_column(Date)
    # When the stage last changed, e.g. "Ghosted on 12 Nov".
    stage_changed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lost_reason_id: Mapped[int | None] = mapped_column(ForeignKey("lost_reasons.id", ondelete="SET NULL"))
    lost_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # The partner that registered / works this lead (approved deal registration).
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id", ondelete="SET NULL"), index=True)

    organisation: Mapped[Organisation] = relationship(back_populates="leads")
    track: Mapped[Track] = relationship()
    stage: Mapped[Stage] = relationship()
    primary_contact: Mapped[Contact | None] = relationship()
    owner: Mapped[User | None] = relationship()
    lost_reason: Mapped[LostReason | None] = relationship()
    product_fit: Mapped[list[Product]] = relationship(secondary=lead_products)
    opportunities: Mapped[list[Opportunity]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


# Indexes on expressions are declared after the classes so they can refer to the real columns.

# Sign-in emails arrive in any case; compare lower-cased so 'Kabir@' and 'kabir@' are one person.
Index("uq_users_email_lower", func.lower(User.email), unique=True)

# Not unique: many colleges share generic names across states. The importer warns instead.
Index("ix_organisations_name_lower", func.lower(Organisation.name))

# One contact per email address, ignoring case; contacts without an email are allowed.
Index(
    "uq_contacts_email_lower",
    func.lower(Contact.email),
    unique=True,
    postgresql_where=Contact.email.isnot(None),
    sqlite_where=Contact.email.isnot(None),
)


class OpportunityProduct(Base):
    """A product on a deal, with its delivery dates (each product's delivery time after the PO)."""

    __tablename__ = "opportunity_products"

    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    expected_delivery_on: Mapped[date | None] = mapped_column(Date)
    delivered_on: Mapped[date | None] = mapped_column(Date)

    opportunity: Mapped[Opportunity] = relationship(back_populates="line_items")
    product: Mapped[Product] = relationship()


class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (CheckConstraint(f"status IN {OPPORTUNITY_STATUSES}", name="status_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    value_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    expected_close: Mapped[date | None] = mapped_column(Date)
    # Purchase-order date: starts the delivery clock for every product on the deal.
    po_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    # Plain text for now; procurement week (Week 6) turns these into config-driven choices.
    funding_source: Mapped[str | None] = mapped_column(String(80))
    buying_route: Mapped[str | None] = mapped_column(String(80))
    budget_window_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    notes: Mapped[str | None] = mapped_column(Text)

    lead: Mapped[Lead] = relationship(back_populates="opportunities")
    line_items: Mapped[list[OpportunityProduct]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )
