"""News: fetch the free feeds (Google News searches, RSS such as PIB), tag each item with its topic and a suggested
signal type, spot lead institutions named in it, and act on items (save as a signal, save to the Library,
bookmark, dismiss).

Everything here is rule-based and free: keyword matching against the headline and summary, no paid API, no AI.
Feeds are fetched when someone opens the News page and the last fetch is older than the refresh interval
(default 6 hours), or when they press Refresh.
"""

from __future__ import annotations

import html
import re
from calendar import timegm
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import pandas as pd
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import AppSetting, AuditLog, Lead, NewsItem, NewsSource, Organisation, SignalType, User
from app.services.cadence import IST
from app.services.signals import (
    SignalError,
    add_signal,
    lead_orgs,
    name_variants,
    normalise,
    recompute_scores,
)

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
USER_AGENT = "Mozilla/5.0 (Demo BD Hub demo; news reader)"
DEFAULT_REFRESH_HOURS = 6
DEFAULT_KEEP_DAYS = 120
_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}

# Words that, on their own, describe many institutions — names made only of these are never matched in news.
GENERIC_WORDS = frozenset("""
government govt college colleges degree science sciences arts commerce engineering institute institution technology
technological of and the for in at university universities polytechnic women womens woman girls boys mahavidyalaya
vidyalaya school model autonomous national state central junior senior first grade pg post graduate centre center
research studies management education educational department dept city district rural urban new public private
academy higher advanced applied studies teachers training memorial trust society india indian international
""".split())


class NewsError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _setting(session: Session, key: str, default: int) -> int:
    row = session.get(AppSetting, key)
    try:
        return int(row.value) if row and row.value else default
    except ValueError:
        return default


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ---------------------------------------------------------------- finding institutions and signal types


class OrgMatcher:
    """Finds lead institutions named in a piece of text by looking up every run of up to 12 words in a table of
    normalised names, short forms and aliases. A name is skipped when it is made only of generic words
    ('Government Degree College') or when it belongs to more than one institution — a wrong match is worse than
    a missed one."""

    def __init__(self, orgs: Iterable[tuple[int, str, list[str]]]) -> None:
        owners: dict[str, set[int]] = {}
        trusted: set[str] = set()
        for org_id, name, aliases in orgs:
            # Aliases and a short form written in brackets ('Indian Institute of Science (IISc)') are deliberate
            # names, so even one word is enough for them.
            trusted |= {normalise(a) for a in aliases if normalise(a)}
            trusted |= {normalise(b) for b in re.findall(r"\(([^)]*)\)", name or "") if normalise(b)}
            for variant in name_variants(name, aliases):
                owners.setdefault(variant, set()).add(org_id)
        self.index: dict[str, int] = {}
        for phrase, ids in owners.items():
            if len(ids) != 1:
                continue
            words = phrase.split()
            if phrase in trusted:
                ok = len(phrase) >= 3
            else:
                ok = len(words) >= 2 and len(phrase) >= 8 and any(w not in GENERIC_WORDS for w in words)
            if ok:
                self.index[phrase] = next(iter(ids))
        self.longest = min(max((len(p.split()) for p in self.index), default=1), 12)

    def find(self, text: str) -> list[int]:
        words = normalise(text).split()
        found: dict[int, None] = {}
        for i in range(len(words)):
            for n in range(1, min(self.longest, len(words) - i) + 1):
                org_id = self.index.get(" ".join(words[i:i + n]))
                if org_id is not None:
                    found[org_id] = None
        return list(found)


def _keyword_pattern(word: str) -> re.Pattern[str]:
    # 'inaugurat*' matches inaugurated / inauguration; other words must match whole.
    word = word.strip().lower()
    if word.endswith("*"):
        return re.compile(r"\b" + re.escape(normalise(word[:-1])))
    return re.compile(r"\b" + re.escape(normalise(word)) + r"\b")


class TypeSuggester:
    """Suggests the highest-weight manual signal type whose headline words appear in the item."""

    def __init__(self, types: Iterable[SignalType]) -> None:
        self.rules = [(t.id, t.weight, [_keyword_pattern(w) for w in (t.keywords or []) if w.strip()])
                      for t in types if t.code is None and t.is_active]
        self.rules.sort(key=lambda r: -r[1])

    def suggest(self, text: str) -> int | None:
        norm = normalise(text)
        for type_id, _, patterns in self.rules:
            if any(p.search(norm) for p in patterns):
                return type_id
        return None


# ---------------------------------------------------------------- reading feeds


def feed_url(source: NewsSource) -> str:
    if source.kind == "rss":
        return (source.url or "").strip()
    query = (source.query or "").strip()
    if "when:" not in query:
        query += " when:30d"  # Google News operator: only the last 30 days
    return GOOGLE_NEWS_URL.format(query=quote_plus(query))


def http_fetch(url: str) -> bytes:
    import requests  # local: only needed when actually fetching

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    response.raise_for_status()
    return response.content


def _clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


@dataclass
class ParsedItem:
    url: str
    title: str
    publisher: str | None
    published_at: datetime | None
    summary: str | None


def parse_feed(data: bytes) -> list[ParsedItem]:
    feed = feedparser.parse(data)
    if feed.bozo and not feed.entries:
        raise NewsError(f"Couldn't read the feed ({type(feed.bozo_exception).__name__}).")
    items = []
    for entry in feed.entries:
        url = (entry.get("link") or "").strip()
        title = _clean(entry.get("title"))
        if not url or not title:
            continue
        source = entry.get("source") or {}
        publisher = _clean(source.get("title")) or None
        # Google News appends ' - Publisher' to every headline; the publisher is shown separately.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(timegm(stamp), tz=timezone.utc) if stamp else None
        summary = _clean(entry.get("summary"))
        # Google News summaries just repeat the headline and publisher.
        if summary and (normalise(summary).startswith(normalise(title)[:40]) or len(summary) < 30):
            summary = ""
        items.append(ParsedItem(url[:1000], title[:500], publisher[:200] if publisher else None, published,
                                summary[:2000] or None))
    return items


@dataclass
class FetchResult:
    checked: int = 0
    new_items: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.checked:
            return "News is up to date."
        text = f"Checked {self.checked} news source(s): {self.new_items} new item(s)."
        if self.errors:
            text += f" {len(self.errors)} source(s) couldn't be read — see Configuration → News & signals."
        return text


def due_sources(session: Session, *, force: bool = False, now: datetime | None = None) -> list[NewsSource]:
    now = now or datetime.now(timezone.utc)
    hours = _setting(session, "news_refresh_hours", DEFAULT_REFRESH_HOURS)
    sources = list(session.scalars(select(NewsSource).where(NewsSource.is_active.is_(True))
                                   .order_by(NewsSource.sort_order, NewsSource.id)))
    if force:
        return sources
    return [s for s in sources if s.last_fetched_at is None or _utc(s.last_fetched_at) < now - timedelta(hours=hours)]


def fetch_news(session: Session, *, force: bool = False, fetch: Callable[[str], bytes] | None = None,
               now: datetime | None = None) -> FetchResult:
    """Reads every due source and stores new items. Network calls run in parallel; database work doesn't."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or http_fetch
    sources = due_sources(session, force=force, now=now)
    result = FetchResult(checked=len(sources))
    if not sources:
        return result

    def grab(source: NewsSource) -> tuple[int, bytes | None, str | None]:
        url = feed_url(source)
        if not url:
            return source.id, None, "No search words or address."
        try:
            return source.id, fetch(url), None
        except Exception as error:  # noqa: BLE001 — any network/HTTP failure is shown on the source, not raised
            return source.id, None, f"{type(error).__name__}: {error}"[:300]

    by_id = {s.id: s for s in sources}
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(grab, sources))

    keep_days = _setting(session, "news_keep_days", DEFAULT_KEEP_DAYS)
    oldest = now - timedelta(days=keep_days)
    matcher = OrgMatcher(lead_orgs(session))
    suggester = TypeSuggester(session.scalars(select(SignalType)))
    known_urls: set[str] = set()
    recent_titles = {normalise(t) for t in session.scalars(
        select(NewsItem.title).where(NewsItem.fetched_at >= now - timedelta(days=45)))}

    for source_id, data, error in fetched:
        source = by_id[source_id]
        source.last_fetched_at = now
        if error is None:
            try:
                parsed = parse_feed(data or b"")
            except NewsError as problem:
                error = str(problem)
        if error is not None:
            source.last_error, source.last_count = error, 0
            result.errors.append(f"{source.name}: {error}")
            continue
        must = [w.strip().lower() for w in (source.must_include or "").split(",") if w.strip()]
        urls = [p.url for p in parsed]
        existing = set(session.scalars(select(NewsItem.url).where(NewsItem.url.in_(urls)))) if urls else set()
        added = 0
        for item in parsed:
            text = f"{item.title} {item.summary or ''}"
            if must and not any(w in text.lower() for w in must):
                continue
            if item.published_at is not None and item.published_at < oldest:
                continue
            title_key = normalise(item.title)
            if item.url in existing or item.url in known_urls or title_key in recent_titles:
                continue  # the same story often comes back from several searches
            known_urls.add(item.url)
            recent_titles.add(title_key)
            session.add(NewsItem(url=item.url, title=item.title, publisher=item.publisher,
                                 published_at=item.published_at, summary=item.summary, source_id=source.id,
                                 topic=source.topic, suggested_type_id=suggester.suggest(text),
                                 matched_org_ids=matcher.find(text) or None, status="new", fetched_at=now))
            added += 1
        source.last_error, source.last_count = None, added
        result.new_items += added
    # Old items nobody acted on are pruned; saved, signal and Library items stay.
    session.execute(delete(NewsItem).where(NewsItem.status.in_(("new", "dismissed")),
                                           func.coalesce(NewsItem.published_at, NewsItem.fetched_at) < oldest))
    session.flush()
    return result


def rematch(session: Session, days: int = 60) -> int:
    """Re-runs institution matching and type suggestions on recent items (after aliases or headline words change)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    matcher = OrgMatcher(lead_orgs(session))
    suggester = TypeSuggester(session.scalars(select(SignalType)))
    changed = 0
    for item in session.scalars(select(NewsItem).where(NewsItem.fetched_at >= since)):
        text = f"{item.title} {item.summary or ''}"
        matched, suggested = matcher.find(text) or None, suggester.suggest(text)
        if matched != item.matched_org_ids or suggested != item.suggested_type_id:
            item.matched_org_ids, item.suggested_type_id = matched, suggested
            changed += 1
    session.flush()
    return changed


# ---------------------------------------------------------------- the News page


def news_items(session: Session, *, statuses: Iterable[str] = ("new", "saved"), topics: Iterable[str] = (),
               leads_only: bool = False, text: str | None = None, limit: int = 200) -> list[dict]:
    query = (select(NewsItem, SignalType.name).outerjoin(SignalType, SignalType.id == NewsItem.suggested_type_id)
             .where(NewsItem.status.in_(list(statuses)))
             .order_by(func.coalesce(NewsItem.published_at, NewsItem.fetched_at).desc(), NewsItem.id.desc()))
    topics = list(topics)
    if topics:
        query = query.where(NewsItem.topic.in_(topics))
    for word in (text or "").split():
        like = f"%{word.replace('%', '').replace('_', '')}%"
        query = query.where(or_(NewsItem.title.ilike(like), NewsItem.summary.ilike(like),
                                NewsItem.publisher.ilike(like)))
    rows = session.execute(query.limit(limit * 3 if leads_only else limit)).all()
    org_ids = {oid for item, _ in rows for oid in (item.matched_org_ids or [])}
    # (name, a lead id to open) per matched institution; an institution in several tracks opens its first lead.
    names = {oid: (name, lead_id) for oid, name, lead_id in session.execute(
        select(Organisation.id, Organisation.name, func.min(Lead.id)).join(Lead, Lead.org_id == Organisation.id)
        .where(Organisation.id.in_(org_ids)).group_by(Organisation.id, Organisation.name))} if org_ids else {}
    out = []
    for item, suggested in rows:
        matched = [(oid, *names[oid]) for oid in (item.matched_org_ids or []) if oid in names]
        if leads_only and not matched:
            continue
        published = item.published_at or item.fetched_at
        out.append({"id": item.id, "title": item.title, "url": item.url, "publisher": item.publisher or "",
                    "published": published.astimezone(IST) if published and published.tzinfo else published,
                    "summary": item.summary or "", "topic": item.topic or "", "status": item.status,
                    "suggested_type_id": item.suggested_type_id, "suggested": suggested or "",
                    "matched": matched, "in_library": item.library_document_id is not None})
        if len(out) >= limit:
            break
    return out


def news_topics(session: Session) -> list[str]:
    return sorted({t for t in session.scalars(select(NewsSource.topic).distinct()) if t})


def unread_lead_news(session: Session, days: int = 14) -> int:
    """New items that name a lead institution — the attention strip under the command bar."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # Counted in Python: 'no match' may be stored as SQL NULL or JSON null depending on the database.
    return sum(1 for ids in session.scalars(select(NewsItem.matched_org_ids).where(
        NewsItem.status == "new", NewsItem.fetched_at >= since)) if ids)


def set_status(session: Session, item_id: int, status: str, user: User) -> None:
    _require(user)
    if status not in ("new", "saved", "dismissed"):
        raise NewsError("Unknown status.")
    item = session.get(NewsItem, item_id)
    if item is None:
        raise NewsError("That news item is gone — refresh the page.")
    if item.status == "signal" and status != "saved":
        return  # an item used for a signal stays marked as used
    item.status, item.actioned_by_user_id, item.actioned_at = status, user.id, datetime.now(timezone.utc)
    session.flush()


def save_as_signal(session: Session, item_id: int, *, org_ids: list[int], type_id: int, observed_on: date,
                   user: User, note: str | None = None) -> int:
    """Saves the item as a signal for each chosen institution. Returns how many signals were added."""
    _require(user)
    item = session.get(NewsItem, item_id)
    if item is None:
        raise NewsError("That news item is gone — refresh the page.")
    if not org_ids:
        raise NewsError("Pick at least one institution.")
    added = 0
    for org_id in dict.fromkeys(org_ids):
        _, created = add_signal(session, org_id=org_id, type_id=type_id, observed_on=observed_on, user=user,
                                source_url=item.url, note=note or item.title, news_item_id=item.id)
        added += created
    item.status, item.actioned_by_user_id, item.actioned_at = "signal", user.id, datetime.now(timezone.utc)
    recompute_scores(session, org_ids=org_ids)
    return added


def save_to_library(session: Session, item_id: int, user: User) -> int:
    """Adds the article to the Library as a 'News clipping' link (a draft, like any new document)."""
    from app.services.library import DocumentData, LibraryError, add_document

    _require(user)
    item = session.get(NewsItem, item_id)
    if item is None:
        raise NewsError("That news item is gone — refresh the page.")
    if item.library_document_id:
        raise NewsError("Already in the Library.")
    when = (item.published_at or item.fetched_at)
    description = " · ".join(x for x in (item.publisher, when.astimezone(IST).strftime("%d %b %Y") if when else None,
                                         item.topic) if x)
    try:
        doc, _ = add_document(session, DocumentData(title=item.title[:200], doc_type="News clipping", url=item.url,
                                                    description=description, tags=item.topic), user)
    except LibraryError as error:
        raise NewsError(str(error)) from error
    item.library_document_id = doc.id
    if item.status == "new":
        item.status = "saved"
    session.flush()
    return doc.id


# ---------------------------------------------------------------- Configuration


SOURCE_COLUMNS = ["id", "Name", "Kind", "Search words", "RSS address", "Topic", "Only keep if it mentions",
                  "Active", "Last checked", "New last time", "Problem"]


def sources_frame(session: Session) -> pd.DataFrame:
    rows = session.scalars(select(NewsSource).order_by(NewsSource.sort_order, NewsSource.id))
    return pd.DataFrame([{
        "id": s.id, "Name": s.name, "Kind": "Google News search" if s.kind == "google_news" else "RSS feed",
        "Search words": s.query or "", "RSS address": s.url or "", "Topic": s.topic,
        "Only keep if it mentions": s.must_include or "", "Active": s.is_active,
        "Last checked": s.last_fetched_at.astimezone(IST).replace(tzinfo=None) if s.last_fetched_at else None,
        "New last time": s.last_count, "Problem": s.last_error or "",
    } for s in rows], columns=SOURCE_COLUMNS)


def save_sources(session: Session, frame: pd.DataFrame, user: User) -> int:
    _require(user, "admin")
    existing = {s.id: s for s in session.scalars(select(NewsSource))}
    kept: set[int] = set()
    changed = 0
    for order, row in enumerate(frame.to_dict("records"), start=1):
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        kind = "rss" if str(row.get("Kind") or "").startswith("RSS") else "google_news"
        query = str(row.get("Search words") or "").strip() or None
        url = str(row.get("RSS address") or "").strip() or None
        topic = str(row.get("Topic") or "").strip()
        if not topic:
            raise NewsError(f"{name}: give it a topic (it's how the feed is filtered).")
        if kind == "google_news" and not query:
            raise NewsError(f"{name}: a Google News search needs search words.")
        if kind == "rss" and not (url and re.match(r"^https?://\S+$", url)):
            raise NewsError(f"{name}: an RSS feed needs an address starting with https://")
        values = {"name": name[:120], "kind": kind, "query": query, "url": url, "topic": topic[:80],
                  "must_include": str(row.get("Only keep if it mentions") or "").strip() or None,
                  "is_active": bool(row.get("Active", True)), "sort_order": order}
        row_id = row.get("id")
        source = existing.get(int(row_id)) if row_id is not None and row_id == row_id else None
        if source is None:
            session.add(NewsSource(**values))
            changed += 1
            continue
        kept.add(source.id)
        if any(getattr(source, k) != v for k, v in values.items()):
            searched = (source.kind, source.query, source.url)
            for k, v in values.items():
                setattr(source, k, v)
            if searched != (source.kind, source.query, source.url):
                source.last_fetched_at = None  # fetch the changed search on the next visit
            changed += 1
    for source_id, source in existing.items():
        if source_id not in kept:
            session.delete(source)  # items keep their text; their source link is cleared
            changed += 1
    if changed:
        session.add(AuditLog(user_id=user.id, entity="news_sources", entity_id="*", action="update",
                             diff={"News sources": [None, f"{changed} change(s)"]}))
        session.flush()
    return changed


def save_org_aliases(session: Session, org_id: int, text: str, user: User) -> list[str]:
    """Other names for an institution, comma-separated (e.g. 'IIT Bombay, IITB')."""
    _require(user)
    org = session.get(Organisation, org_id)
    if org is None:
        raise SignalError("That institution no longer exists.")
    aliases = list(dict.fromkeys(a.strip() for a in text.split(",") if a.strip()))[:20]
    before = org.aliases or []
    org.aliases = aliases or None
    session.add(AuditLog(user_id=user.id, entity="organisations", entity_id=str(org_id), action="update",
                         diff={"Also known as": [", ".join(before) or None, ", ".join(aliases) or None]}))
    session.flush()
    return aliases


if __name__ == "__main__":
    # Check every news source from the terminal:  python -m app.services.news
    from app.db import session_scope

    with session_scope() as _session:
        _result = fetch_news(_session, force=True)
    print(_result.summary())
    for _problem in _result.errors:
        print(" -", _problem)
