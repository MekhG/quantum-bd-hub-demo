"""Small privacy helpers used by the suppression list and, later, the importer."""

from __future__ import annotations

import hashlib


def normalise_email(email: str) -> str:
    return email.strip().lower()


def email_hash(email: str) -> str:
    """SHA-256 of the normalised email: lets us block an address without storing it."""
    return hashlib.sha256(normalise_email(email).encode("utf-8")).hexdigest()
