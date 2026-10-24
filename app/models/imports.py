"""Import log: every upload is recorded, and the rows it created can be undone."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TZDateTime

IMPORT_KINDS = ("tracker", "responses", "clients", "colleges")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    file_name: Mapped[str] = mapped_column(String(255))
    sheet_name: Mapped[str | None] = mapped_column(String(120))
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    imported_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    imported_at: Mapped[datetime] = mapped_column(TZDateTime(), server_default=func.now())
    rows_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Counts shown in the import history, e.g. [["Contacts created", 42], ...].
    summary: Mapped[list[Any] | None] = mapped_column(JSONType)
    undone_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    undone_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    items: Mapped[list[ImportBatchItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ImportBatchItem(Base):
    """One row this import CREATED (updates to existing rows are not listed and not undone)."""

    __tablename__ = "import_batch_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    entity: Mapped[str] = mapped_column(String(40))  # table name
    entity_id: Mapped[int] = mapped_column(Integer)

    batch: Mapped[ImportBatch] = relationship(back_populates="items")
