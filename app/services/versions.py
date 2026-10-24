"""Row 'versions' for the conflict check: a fingerprint of when a row was last changed.

The table remembers each row's version when it loads; on save, a row whose version has moved on was
changed by someone else meanwhile, so it's skipped instead of being silently overwritten.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _stamp(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "-"
    ts = pd.Timestamp(value)
    return str((ts.tz_convert("UTC") if ts.tzinfo else ts).value)


def version_of(*stamps: Any) -> str:
    return "|".join(_stamp(s) for s in stamps)


def version_series(*columns: pd.Series) -> pd.Series:
    """Vectorised version_of for whole DataFrame columns (fast for 50,000 rows)."""
    parts = []
    for column in columns:
        stamps = pd.to_datetime(column, utc=True).dt.as_unit("ns")
        text = stamps.astype("int64").astype(str)
        parts.append(text.where(stamps.notna(), "-"))
    out = parts[0]
    for part in parts[1:]:
        out = out + "|" + part
    return out
