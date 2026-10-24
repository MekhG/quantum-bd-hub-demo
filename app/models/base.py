"""Shared base class and column helpers for all tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Predictable constraint names let Alembic alter or drop constraints later without guessing.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres (indexable, compact); plain JSON elsewhere so tests can run on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class TZDateTime(TypeDecorator):
    """A timestamp that always comes back time-zone aware.

    Postgres stores the zone itself. SQLite (the demo's database, and the tests') would drop it and hand back a naive
    value, so a 10:00 IST task would read back as 10:00 UTC. Here every value is stored in UTC and read back as UTC,
    so the app's IST conversions are right on both databases.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        if value.tzinfo is None:  # naive values in this app are UTC
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
