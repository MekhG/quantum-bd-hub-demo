"""Events & community (Week 9): hackathons, workshops and other events that aren't tied to one lead, the community
of people who take part, and who took part in what.

A community member is identified by email (one person across many events). Members are mailable only with
consent AND a confirmation that they are 18 or older (DPDP: children's data needs a parent's consent); everyone
else stays on the event record but is left out of mailing lists and outreach exports.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.core import Organisation, User

EVENT_MODES = ("online", "offline", "hybrid")
EVENT_STATUSES = ("planned", "open", "done", "cancelled")
PARTICIPANT_ROLES = ("participant", "mentor", "judge", "speaker", "volunteer", "organiser")
PARTICIPANT_STATUSES = ("registered", "attended", "no_show", "finalist", "winner")


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(f"mode IN {EVENT_MODES}", name="mode_valid"),
        CheckConstraint(f"status IN {EVENT_STATUSES}", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Hackathon, Workshop, Webinar, FDP, Conference, Expo… (option list 'event_kind').
    kind: Mapped[str] = mapped_column(String(40))
    start_on: Mapped[date] = mapped_column(Date, index=True)
    end_on: Mapped[date | None] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(10), default="online", server_default="online")
    venue: Mapped[str | None] = mapped_column(String(255))
    partner: Mapped[str | None] = mapped_column(String(120))      # e.g. a training partner, a host university
    form_url: Mapped[str | None] = mapped_column(String(1000))    # the Google Form people registered with
    cost_inr: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(12), default="planned", server_default="planned")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    owner: Mapped[User | None] = relationship()


class CommunityMember(TimestampMixin, Base):
    __tablename__ = "community_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    # As they wrote it ("B.Tech ECE, 3rd year"); level is the standard group worked out from it.
    education: Mapped[str | None] = mapped_column(String(255))
    level: Mapped[str | None] = mapped_column(String(40))          # option list 'community_level'
    institution: Mapped[str | None] = mapped_column(String(255))   # as written in the form
    # The institution in the hub, when the name matched one (links a member to leads and signals).
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="SET NULL"), index=True)
    city: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(60))
    interests: Mapped[str | None] = mapped_column(Text)
    github_url: Mapped[str | None] = mapped_column(String(500))
    website_url: Mapped[str | None] = mapped_column(String(500))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    project_links: Mapped[str | None] = mapped_column(Text)        # one per line
    # Mailable only when both are true and the member isn't on the do-not-contact list.
    consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    consent_on: Mapped[date | None] = mapped_column(Date)
    is_adult: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    source: Mapped[str | None] = mapped_column(String(160))        # e.g. "Google Form — Quantum Hackathon 2026"
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    notes: Mapped[str | None] = mapped_column(Text)

    organisation: Mapped[Organisation | None] = relationship()


# One member per email address, ignoring case.
Index("uq_community_members_email_lower", func.lower(CommunityMember.email), unique=True)


class EventParticipant(Base):
    __tablename__ = "event_participants"
    __table_args__ = (
        UniqueConstraint("event_id", "member_id", name="uq_event_participants_event_member"),
        CheckConstraint(f"role IN {PARTICIPANT_ROLES}", name="role_valid"),
        CheckConstraint(f"status IN {PARTICIPANT_STATUSES}", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("community_members.id", ondelete="CASCADE"), index=True)
    team_name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(20), default="participant", server_default="participant")
    status: Mapped[str] = mapped_column(String(20), default="registered", server_default="registered")
    project_title: Mapped[str | None] = mapped_column(String(255))
    project_link: Mapped[str | None] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    event: Mapped[Event] = relationship()
    member: Mapped[CommunityMember] = relationship()
