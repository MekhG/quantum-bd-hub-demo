"""Settings loaded from .env locally, or from Streamlit secrets when deployed.

The demo needs no secrets at all: without a DATABASE_URL it builds its own SQLite database (data/demo.db) full of
fictional data and rebuilds it every day. A DATABASE_URL (e.g. a throwaway Postgres) can still be supplied instead.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DB_PATH = PROJECT_ROOT / "data" / "demo.db"


def _from_streamlit_secrets(key: str) -> str | None:
    # Imported lazily so command-line scripts (migrations, seeding) don't need Streamlit running.
    try:
        import streamlit as st

        value = st.secrets.get(key)
        return str(value) if value else None
    except Exception:
        return None


def get_setting(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or _from_streamlit_secrets(key) or default


def normalise_url(url: str) -> str:
    # Supabase hands out 'postgresql://...'; SQLAlchemy needs to be told to use the psycopg 3 driver.
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def get_database_url() -> str:
    url = get_setting("DATABASE_URL")
    if not url:
        # The portfolio demo's own database: a local SQLite file, rebuilt daily (see app/demo/refresh.py).
        return f"sqlite:///{DEMO_DB_PATH.as_posix()}"
    return normalise_url(url)


def uses_demo_sqlite() -> bool:
    return get_database_url().startswith("sqlite:///")


def get_dev_user_email() -> str | None:
    """Email to act as when running locally without the demo's persona picker (tests, scripts)."""
    return get_setting("DEV_USER_EMAIL")


def is_demo() -> bool:
    """The portfolio demo: visitors pick a fictional persona instead of signing in. On unless DEMO_MODE=0."""
    return (get_setting("DEMO_MODE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
