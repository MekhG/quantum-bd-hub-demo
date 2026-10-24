"""'Export everything': every table in one Excel file, as a weekly backup.

The free Supabase plan keeps no backups, so the admin downloads this and stores it outside the
project folder (it contains contact data, so it must never go into Git).
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Base

_SKIP = {"alembic_version"}


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def export_everything(session: Session, user_id: int | None) -> tuple[bytes, dict[str, int]]:
    """Returns the .xlsx bytes and the row count per table."""
    buffer = BytesIO()
    counts: dict[str, int] = {}
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for table in Base.metadata.sorted_tables:
            if table.name in _SKIP:
                continue
            rows = session.execute(select(table)).mappings().all()
            frame = pd.DataFrame([{k: _cell(v) for k, v in row.items()} for row in rows],
                                 columns=[c.name for c in table.columns])
            for column in frame.columns:
                if isinstance(frame[column].dtype, pd.DatetimeTZDtype):  # Excel can't store time zones
                    frame[column] = frame[column].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                elif frame[column].dtype == object:
                    frame[column] = frame[column].map(
                        lambda v: v.astimezone().replace(tzinfo=None) if hasattr(v, "tzinfo") and v.tzinfo else v
                    )
            frame.to_excel(writer, sheet_name=table.name[:31], index=False)
            counts[table.name] = len(frame)
    session.add(AuditLog(user_id=user_id, entity="all", action="export", diff={"tables": counts}))
    return buffer.getvalue(), counts
