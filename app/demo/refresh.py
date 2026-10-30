"""Keeps the demo database fresh: rebuilt once a day (IST), so dates always end 'today' and visitors' edits reset.

The build runs in a separate Python process (it moves the clock while it works, which must never affect the running
app), writes to a temporary file, and is then swapped in. A lock file stops two visitors from building at once.

Nobody waits for it once data exists: `start_refresh` builds today's data on a background thread while visitors
keep using yesterday's, and swaps it in when it's ready. Only a brand-new server (no data at all) has to wait, and
even then the build starts the moment the app wakes, while the visitor is still reading the welcome page.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time as clock_time
from collections.abc import Callable
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


def schema_fingerprint() -> str:
    """Short hash of every table and column. A database built by older code (say, before a column was added) has a
    different one, so it is never served: a redeploy that changes the tables rebuilds before anyone sees errors."""
    from app.models import Base

    names = sorted(f"{t.name}.{c.name}" for t in Base.metadata.tables.values() for c in t.columns)
    return hashlib.sha1("|".join(names).encode()).hexdigest()[:12]


def built_for() -> date | None:
    """The day the database was built for, or None if there is none (or it was built by older code)."""
    try:
        if not DEMO_DB_PATH.exists():
            return None
        day, _, schema = STAMP.read_text().strip().partition("|")
        return date.fromisoformat(day) if schema == schema_fingerprint() else None
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
        STAMP.write_text(f"{day.isoformat()}|{schema_fingerprint()}")
        return True
    finally:
        _release_lock()


# ---------------------------------------------------------------- background refresh (the running app uses these)

_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_last_error: str | None = None
_started_at: float | None = None
TYPICAL_BUILD_SECONDS = 25  # for the progress bar on a brand-new server


def has_data() -> bool:
    """True when some demo database (today's or an older one) is there to serve."""
    return DEMO_DB_PATH.exists() and built_for() is not None


def is_fresh(day: date | None = None) -> bool:
    return built_for() == (day or today_ist())


def is_building() -> bool:
    return _worker is not None and _worker.is_alive()


def build_error() -> str | None:
    return _last_error


def seconds_building() -> float:
    return clock_time.monotonic() - _started_at if _started_at is not None and is_building() else 0.0


def start_refresh(day: date | None = None, *, on_swap: Callable[[], None] | None = None) -> bool:
    """Starts building today's data on a background thread, unless it's already there or being built. Never
    waits. Returns True if a build is running after the call."""
    global _worker, _started_at, _last_error
    if not uses_demo_sqlite():
        return False
    day = day or today_ist()
    if built_for() == day:
        return False
    with _worker_lock:
        if is_building():
            return True
        _last_error = None
        _started_at = clock_time.monotonic()
        _worker = threading.Thread(target=_refresh_worker, args=(day, on_swap), name="demo-refresh", daemon=True)
        _worker.start()
    return True


def _refresh_worker(day: date, on_swap: Callable[[], None] | None) -> None:
    global _last_error
    try:
        if ensure_fresh(day, on_swap=on_swap) and on_swap is not None:
            on_swap()  # again after the swap: nothing may keep a connection to the old file
    except Exception as error:  # noqa: BLE001 — shown on the waiting screen; the next visit tries again
        _last_error = str(error)[-1500:]


def wait_for_data(timeout: float) -> bool:
    """Blocks up to `timeout` seconds for the running build. True once some data is there."""
    worker = _worker
    if worker is not None:
        worker.join(timeout)
    return has_data()
