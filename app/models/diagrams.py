"""GTM charts (Week 10): diagrams made in the diagram builder.

A diagram is stored as its text outline (the same text people type or a template fills in), so it can always be
edited, duplicated or redrawn — nothing is stored as a picture.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Diagram(TimestampMixin, Base):
    __tablename__ = "diagrams"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # Which template it started from: cadence, partner, reseller, custom.
    kind: Mapped[str] = mapped_column(String(20), default="custom", server_default="custom")
    outline: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
