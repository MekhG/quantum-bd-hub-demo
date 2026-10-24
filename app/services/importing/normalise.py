"""Cleaning helpers: turn messy spreadsheet cells into consistent values."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

_EMPTY = {"", "nan", "none", "null", "-", "--", "—", "–", "n/a", "na", "nil", "#n/a", "not available"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Personal mailboxes say nothing about which institution someone belongs to.
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "rediffmail.com", "rediff.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "aol.com", "zoho.com", "zohomail.in",
}

# Second-level suffixes: 'ac.in' alone is never an institution's domain.
_SHARED_SUFFIXES = {
    "ac.in", "edu.in", "co.in", "org.in", "gov.in", "res.in", "nic.in", "ernet.in", "net.in",
    "gen.in", "firm.in", "ind.in", "ac.uk", "co.uk", "edu.au",
}

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan",
    "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]
_STATE_VARIANTS = {
    "orissa": "Odisha",
    "pondicherry": "Puducherry",
    "uttaranchal": "Uttarakhand",
    "new delhi": "Delhi",
    "nct of delhi": "Delhi",
    "j and k": "Jammu and Kashmir",
    "jammu kashmir": "Jammu and Kashmir",
    "andaman and nicobar": "Andaman and Nicobar Islands",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "chattisgarh": "Chhattisgarh",
    "tamilnadu": "Tamil Nadu",
    "telengana": "Telangana",
}


def clean(value: Any) -> str | None:
    """Cell → trimmed text, or None for blanks and placeholders like 'NA' or '-'."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return None if text.lower() in _EMPTY else text


def norm_key(text: str | None) -> str:
    """Comparison key: lower-case, '&' → 'and', punctuation dropped, spaces collapsed."""
    if not text:
        return ""
    text = text.lower().replace("&", " and ").replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9%+\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_parentheses(text: str) -> str:
    return re.sub(r"\(.*?\)", " ", text)


# ---------- people ----------


def parse_emails(text: str | None) -> tuple[str | None, list[str], bool]:
    """Returns (first valid email lower-cased, any extra emails, whether the cell had text but no valid email)."""
    if not text:
        return None, [], False
    found = [e.lower().strip(".") for e in EMAIL_RE.findall(text)]
    if not found:
        return None, [], True
    unique = list(dict.fromkeys(found))
    return unique[0], unique[1:], False


def parse_phones(text: str | None) -> tuple[str | None, list[str]]:
    """First phone number cleaned (10-digit Indian mobiles become +91…); extras returned separately."""
    if not text:
        return None, []
    parts = [p for p in re.split(r"[,;/|\n]+", text) if p.strip()]
    cleaned: list[str] = []
    for part in parts:
        digits = re.sub(r"[^\d+]", "", part)
        if not digits:
            continue
        bare = digits.lstrip("+")
        if len(bare) == 10 and bare[0] in "6789":
            digits = "+91" + bare
        elif len(bare) == 12 and bare.startswith("91"):
            digits = "+" + bare
        elif len(bare) == 11 and bare.startswith("0"):
            digits = bare  # landline with STD code
        cleaned.append(digits)
    if not cleaned:
        return text[:40], []
    return cleaned[0][:40], cleaned[1:]


# ---------- institutions ----------


def domain_from_website(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    host = (urlparse(candidate).hostname or "").lower()
    host = host.removeprefix("www.")
    return host if "." in host else None


def domain_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower()
    return None if domain in GENERIC_EMAIL_DOMAINS else domain


def domain_candidates(domain: str | None) -> list[str]:
    """'physics.iitb.ac.in' → ['physics.iitb.ac.in', 'iitb.ac.in'] (never just 'ac.in')."""
    if not domain:
        return []
    labels = domain.split(".")
    candidates = []
    for start in range(len(labels) - 1):
        candidate = ".".join(labels[start:])
        if candidate in _SHARED_SUFFIXES or candidate.count(".") == 0:
            break
        candidates.append(candidate)
    return candidates


_NAME_FILLER = {
    "of", "the", "and", "college", "engineering", "institute", "institution", "technology", "university",
    "science", "sciences", "technical", "education", "school", "studies", "for", "in", "at", "arts",
    "commerce", "management", "research", "centre", "center", "dept", "department", "govt", "government",
}


def names_look_alike(a: str | None, b: str | None) -> bool:
    """True if two institution names plausibly mean the same place ('JNTU Hyderabad' vs 'JNTUH College…')."""
    ka, kb = norm_key(a), norm_key(b)
    if not ka or not kb:
        return False
    if ka == kb or ka in kb or kb in ka:
        return True
    ta = {t for t in ka.split() if t not in _NAME_FILLER}
    tb = {t for t in kb.split() if t not in _NAME_FILLER}
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def match_state(text: str | None) -> str | None:
    key = norm_key(text)
    if not key:
        return None
    for state in INDIAN_STATES:
        if norm_key(state) == key:
            return state
    return _STATE_VARIANTS.get(key)


def parse_location(text: str | None) -> tuple[str | None, str | None]:
    """'Hyderabad, Telangana' → ('Hyderabad', 'Telangana'); 'Telangana' → (None, 'Telangana')."""
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"[,/|-]", text) if p.strip()]
    state = None
    city_parts: list[str] = []
    for part in parts:
        found = match_state(part)
        if found and state is None:
            state = found
        else:
            city_parts.append(part)
    city = ", ".join(city_parts) if city_parts else None
    return (city[:80] if city else None), state


_AISHE_PREFIX_TYPES = {"C": "college", "S": "standalone", "U": "university"}


def org_type_from_aishe(code: str | None) -> str | None:
    """AISHE codes start with C- (college), S- (standalone) or U- (university): the most reliable type."""
    if not code:
        return None
    match = re.match(r"\s*([CSU])\s*-", code.upper())
    return _AISHE_PREFIX_TYPES[match[1]] if match else None


AREAS = {"rural": "Rural", "urban": "Urban", "semi urban": "Semi-urban", "semi-urban": "Semi-urban"}


def match_area(text: str | None) -> str | None:
    return AREAS.get(norm_key(text)) if text else None


def match_org_type(text: str | None) -> str | None:
    """College / Standalone / University wording (including AISHE's longer labels)."""
    key = norm_key(text)
    if not key:
        return None
    if "standalone" in key or "stand alone" in key:
        return "standalone"
    if "college" in key:
        return "college"
    if "universit" in key or "deemed" in key or key in ("iit", "nit"):
        return "university"
    if "institute" in key:
        return "standalone"
    return None


# ---------- dates and numbers ----------

_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value: Any) -> date | None:
    """Accepts 18/10/2026, 18-10-2026, 2026-10-18, '18 Oct 2026', Excel serial numbers. Day comes first."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    if not text:
        return None
    # Excel/Sheets serial day numbers (e.g. 46313).
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        return _EXCEL_EPOCH + timedelta(days=int(float(text)))
    iso = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    from dateutil import parser

    try:
        return parser.parse(text, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def parse_inr(text: str | None) -> float | None:
    """'₹ 12,50,000' → 1250000.0; '12.5 lakh' → 1250000.0."""
    if not text:
        return None
    lowered = text.lower()
    multiplier = 1.0
    if "lakh" in lowered or "lac" in lowered:
        multiplier = 1e5
    elif "crore" in lowered or "cr" in lowered.split():
        multiplier = 1e7
    number = re.sub(r"[^\d.]", "", lowered)
    try:
        return float(number) * multiplier if number else None
    except ValueError:
        return None


def parse_yes_no(text: str | None) -> bool | None:
    key = norm_key(text)
    if key in {"yes", "y", "true", "1", "applied", "nqm"}:
        return True
    if key in {"no", "n", "false", "0"}:
        return False
    return None


def split_list(text: str | None, *, slash: bool = True) -> list[str]:
    """Splits 'QSim, NetSim / QKD kit' into separate items (never on '&', which product names use).

    slash=False keeps 'Faculty Training/Enablement' together.
    """
    if not text:
        return []
    pattern = r"[,;|/\n]+|\s\+\s" if slash else r"[,;|\n]+|\s\+\s"
    return [p.strip() for p in re.split(pattern, text) if p.strip()]


# ---------- dropdown matching ----------


def _potential_key(text: str) -> str:
    return norm_key(text).replace(" ", "").replace("to", "-")


class OptionMatcher:
    """Matches free text to one of a dropdown's values using the value and its aliases.

    Exact match first; then, if exactly one value or alias appears as a whole word
    inside the text ('Dept. of Physics' → 'Physics'), that value.
    """

    def __init__(self, entries: list[tuple[str, list[str] | None]], compact: bool = False,
                 priority: list[str] | None = None) -> None:
        self._compact = compact
        # When text names two values ('HOD / Professor'), the one earlier in `priority` wins.
        self._priority = priority or []
        self._exact: dict[str, str] = {}
        self._words: list[tuple[str, str]] = []
        for value, aliases in entries:
            for variant in [value, *(aliases or [])]:
                key = self._key(variant)
                if key:
                    self._exact.setdefault(key, value)
                    self._words.append((key, value))
        self.values = [value for value, _ in entries]

    def _key(self, text: str) -> str:
        return _potential_key(text) if self._compact else norm_key(text)

    def match(self, text: str | None) -> str | None:
        if not text:
            return None
        key = self._key(text)
        for variant in (key, key.replace("-", " "), key.replace("-", "")):
            if variant in self._exact:
                return self._exact[variant]
        if self._compact:
            return None
        # Drop filler words, then look for a single whole-word hit.
        key = re.sub(r"\b(department|dept|dept of|of|the|school|centre|center|division)\b", " ", key)
        key = re.sub(r"\s+", " ", key).strip()
        if key in self._exact:
            return self._exact[key]
        hits = {
            value
            for word, value in self._words
            if len(word) > 2 and re.search(rf"(^|\s){re.escape(word)}(\s|$)", key)
        }
        if len(hits) == 1:
            return hits.pop()
        ranked = [value for value in self._priority if value in hits]
        return ranked[0] if ranked else None
