"""Keeps the demo database fresh: rebuilt once a day (IST), so dates always end 'today' and visitors' edits reset.

The build runs in a separate Python process (it moves the clock while it works, which must never affect the running
app), writes to a temporary file, and is then swapped in. A lock file stops two visitors from building at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time as clock_time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import DEMO_DB_PATH, PROJECT_ROOT, uses_demo_sqlite

IST = ZoneInfo("Asia/Kolkata")
STAMP = DEMO_DB_PATH.with_suffix(".built")   # holds the IST date the database was built for
LOCK = DEMO_DB_PATH.with_suffix(".lock")
STALE_LOCK_SECONDS = 600


def today_ist() -> date:
    return datetime.now(IST).date()


def built_for() -> date | None:
    try:
        return date.fromisoformat(STAMP.read_text().strip()) if DEMO_DB_PATH.exists() else None
    except (OSError, ValueError):
        return None


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK.exists() and clock_time.time() - LOCK.stat().st_mtime > STALE_LOCK_SECONDS:
            LOCK.unlink()  # a build that crashed long ago
    except OSError:
        pass
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass


def _build_into(path: Path, day: date) -> None:
    subprocess.run([sys.executable, "-m", "app.demo.build", "--out", str(path), "--today", day.isoformat(), "--quiet"],
                   cwd=PROJECT_ROOT, check=True, timeout=900, capture_output=True, text=True)


def ensure_fresh(day: date | None = None, *, on_swap=None) -> bool:
    """Builds today's database if needed. Returns True when a new one was swapped in.
    on_swap: called just before the swap, to close the running app's connections to the old file."""
    if not uses_demo_sqlite():
        return False  # a DATABASE_URL was given: that database is rebuilt by `python -m app.demo.build --url …`
    day = day or today_ist()
    if built_for() == day:
        return False
    while not _acquire_lock():
        clock_time.sleep(1)  # someone else is building; wait for it, then re-check
        if built_for() == day:
            return False
    try:
        if built_for() == day:
            return False
        tmp = DEMO_DB_PATH.with_name(f"demo-{day.isoformat()}-{os.getpid()}.tmp.db")
        try:
            _build_into(tmp, day)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Building the demo data failed:\n{error.stderr[-2000:]}") from error
        if on_swap is not None:
            on_swap()
        for leftover in (DEMO_DB_PATH.with_name(DEMO_DB_PATH.name + "-wal"),
                         DEMO_DB_PATH.with_name(DEMO_DB_PATH.name + "-shm")):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
        os.replace(tmp, DEMO_DB_PATH)
        STAMP.write_text(day.isoformat())
        return True
    finally:
        _release_lock()
