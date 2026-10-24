"""Week 4: contacts.email_status ('bounced' when an email came back undeliverable).

The Inbound queries track, its cadence, task owners and the new lost reasons are configuration data,
added by `python -m app.seed.seed_config`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 00:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("email_status", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "email_status")
