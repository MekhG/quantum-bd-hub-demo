"""Content calendar: idea → draft → review → approved (admin) → posted → engagement a week later.

The hub doesn't publish to LinkedIn or Instagram itself; whoever is the poster copies the approved text, posts it,
then marks it posted with the link. Seven days later a task asks for the likes, comments, reposts and impressions,
so the weekly report can show which themes do best.

Approval is admin-only. Changing the text of an approved post sends it back to review, so what goes out is always
what was approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, ContentPost, Event, NewsItem, OptionList, User
from app.models.content import POST_PLATFORMS, POST_STATUSES
from app.services.cadence import today_ist
from app.services.library import content_flags
from app.services.tasks import cancel_tagged_tasks, complete_tagged_tasks, plan_tagged_task

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
METRICS = ("likes", "comments", "reposts", "impressions")
STATUS_LABELS = {"idea": "Idea", "draft": "Draft", "review": "Waiting for approval", "approved": "Approved",
                 "posted": "Posted", "dropped": "Dropped"}
PLATFORM_LABELS = {"linkedin": "LinkedIn", "instagram": "Instagram", "both": "LinkedIn + Instagram"}
PLATFORM_SHORT = {"linkedin": "LI", "instagram": "IG", "both": "LI+IG"}
METRICS_AFTER_DAYS = 7
# Text changes that send an approved post back for approval; the date, poster or campaign don't.
_APPROVED_FIELDS = ("title", "platform", "body", "hashtags")


class ContentError(ValueError):
    """Shown to the person as-is."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


def _audit(session: Session, user: User, post_id: int, action: str, diff: dict) -> None:
    session.add(AuditLog(user_id=user.id, entity="content_posts", entity_id=str(post_id), action=action, diff=diff))


def _tag(post_id: int, kind: str) -> str:
    return f"[post:{post_id}:{kind}]"


@dataclass
class PostData:
    title: str
    platform: str = "linkedin"
    theme: str | None = None
    campaign: str | None = None
    planned_on: date | None = None
    body: str | None = None
    hashtags: str | None = None
    asset_links: str | None = None
    document_ids: list[int] = field(default_factory=list)
    poster_user_id: int | None = None
    source_kind: str | None = None
    source_id: int | None = None


def _clean(data: PostData) -> PostData:
    data.title = (data.title or "").strip()
    if not data.title:
        raise ContentError("Give the post a short title (only the team sees it).")
    if data.platform not in POST_PLATFORMS:
        raise ContentError("Pick LinkedIn, Instagram or both.")
    data.title = data.title[:200]
    for name in ("theme", "campaign", "body", "asset_links"):
        value = getattr(data, name)
        setattr(data, name, value.strip() if isinstance(value, str) and value.strip() else None)
    data.hashtags = normalise_hashtags(data.hashtags)
    return data


def normalise_hashtags(text: str | None) -> str | None:
    """'quantum, NQM #QuantumIndia' → '#quantum #NQM #QuantumIndia' (no duplicates, order kept)."""
    if not text:
        return None
    seen: list[str] = []
    for word in re.split(r"[\s,;]+", text):
        word = word.strip().lstrip("#")
        word = re.sub(r"[^\w]", "", word)
        if word and word.lower() not in {s.lower() for s in seen}:
            seen.append(word)
    return " ".join(f"#{w}" for w in seen) or None


def warnings_for(session: Session, post: ContentPost | PostData) -> list[str]:
    """Things to fix before approval: flagged words, hardware specifics, Instagram length, missing pieces."""
    notes: list[str] = []
    for word, suggestion in content_flags(session, post.title, post.body, post.hashtags):
        notes.append(f"“{word}”" + (f" — use “{suggestion}”" if suggestion else " — check this wording"))
    joined = "\n".join(t for t in (post.body, post.hashtags) if t)
    for word, aliases in session.execute(
            select(OptionList.value, OptionList.aliases).where(
                OptionList.list_name == "marketing_hardware_words", OptionList.is_active.is_(True))):
        if joined and re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", joined, re.IGNORECASE):
            generic = (aliases or [""])[0] if aliases else ""
            notes.append(f"“{word}” names a kit part — in posts say “{generic or 'a generic term'}”")
    if re.search(r"jupyter\s*hub", joined, re.IGNORECASE):
        notes.append("“JupyterHub” — QSim Cluster runs on Demo's own “Demo Notebook Studio”")
    if not post.body:
        notes.append("There is no post text yet.")
    if post.platform in ("instagram", "both") and post.body and len(post.body) > 2200:
        notes.append(f"Instagram captions stop at 2,200 characters (this is {len(post.body):,}).")
    if post.platform in ("linkedin", "both") and post.body and len(post.body) > 3000:
        notes.append(f"LinkedIn posts stop at 3,000 characters (this is {len(post.body):,}).")
    if post.hashtags and len(post.hashtags.split()) > 10:
        notes.append("More than 10 hashtags tends to look like spam — 3 to 5 is usual.")
    return notes


def create_post(session: Session, data: PostData, user: User) -> ContentPost:
    _require(user)
    data = _clean(data)
    post = ContentPost(
        title=data.title, platform=data.platform, theme=data.theme, campaign=data.campaign,
        planned_on=data.planned_on, body=data.body, hashtags=data.hashtags, asset_links=data.asset_links,
        document_ids=data.document_ids or None, author_user_id=user.id,
        poster_user_id=data.poster_user_id or user.id, source_kind=data.source_kind, source_id=data.source_id,
        status="draft" if data.body else "idea")
    session.add(post)
    session.flush()
    _audit(session, user, post.id, "create", {"Post": [None, post.title]})
    return post


def update_post(session: Session, post_id: int, data: PostData, user: User) -> str | None:
    """Saves edits. Returns a message when the edit changed the post's status."""
    _require(user)
    post = _get(session, post_id)
    if post.status == "posted":
        raise ContentError("This post is already live — only its engagement numbers can change now.")
    data = _clean(data)
    changed_text = any(getattr(post, name) != getattr(data, name) for name in _APPROVED_FIELDS)
    for name in ("title", "platform", "theme", "campaign", "planned_on", "body", "hashtags", "asset_links"):
        setattr(post, name, getattr(data, name))
    post.document_ids = data.document_ids or None
    if data.poster_user_id:
        post.poster_user_id = data.poster_user_id
    message = None
    if post.status == "idea" and post.body:
        post.status = "draft"
    if post.status == "approved":
        if changed_text and user.role != "admin":
            post.status = "review"
            post.approved_by_user_id = post.approved_on = None
            cancel_tagged_tasks(session, _tag(post.id, "publish"))
            message = "The text changed, so the post went back for approval."
        else:
            _plan_publish_task(session, post, user)   # the date or poster may have moved
    _audit(session, user, post.id, "update", {"Post": [None, "edited"]})
    session.flush()
    return message


def _get(session: Session, post_id: int) -> ContentPost:
    post = session.get(ContentPost, post_id)
    if post is None:
        raise ContentError("That post no longer exists.")
    return post


def send_for_review(session: Session, post_id: int, user: User) -> list[str]:
    """Moves a draft to the approval queue. Returns the warnings the admin will see (not blocking)."""
    _require(user)
    post = _get(session, post_id)
    if post.status not in ("idea", "draft"):
        raise ContentError(f"This post is {STATUS_LABELS[post.status].lower()}.")
    if not post.body:
        raise ContentError("Write the post text before sending it for approval.")
    post.status = "review"
    post.review_comment = None
    _audit(session, user, post.id, "update", {"Status": ["draft", "review"]})
    session.flush()
    return warnings_for(session, post)


def approve_post(session: Session, post_id: int, user: User, *, planned_on: date | None = None,
                 poster_user_id: int | None = None) -> str:
    _require(user, "admin")
    post = _get(session, post_id)
    if post.status not in ("review", "draft"):
        raise ContentError(f"This post is {STATUS_LABELS[post.status].lower()}.")
    if not post.body:
        raise ContentError("There is no post text to approve.")
    post.planned_on = planned_on or post.planned_on or today_ist()
    if poster_user_id:
        post.poster_user_id = poster_user_id
    post.poster_user_id = post.poster_user_id or post.author_user_id or user.id
    post.status = "approved"
    post.approved_by_user_id = user.id
    post.approved_on = today_ist()
    post.review_comment = None
    _audit(session, user, post.id, "update", {"Status": ["review", "approved"]})
    session.flush()
    _plan_publish_task(session, post, user)
    poster = session.get(User, post.poster_user_id)
    return f"Approved — {poster.name if poster else 'the poster'} will see it on Today on {post.planned_on:%d %b}."


def _plan_publish_task(session: Session, post: ContentPost, user: User) -> None:
    day = max(post.planned_on or today_ist(), today_ist())
    plan_tagged_task(session, tag=_tag(post.id, "publish"), title=f"Post on {PLATFORM_LABELS[post.platform]}: "
                     f"{post.title}"[:200], day=day, owner_id=post.poster_user_id or user.id, user=user,
                     note="Copy the approved text from the Content page, post it, then mark it posted with the link.")


def send_back(session: Session, post_id: int, comment: str, user: User) -> None:
    _require(user, "admin")
    post = _get(session, post_id)
    if not (comment or "").strip():
        raise ContentError("Say what to change, so the author knows.")
    if post.status not in ("review", "approved"):
        raise ContentError(f"This post is {STATUS_LABELS[post.status].lower()}.")
    post.status = "draft"
    post.review_comment = comment.strip()
    post.approved_by_user_id = post.approved_on = None
    cancel_tagged_tasks(session, _tag(post.id, "publish"))
    _audit(session, user, post.id, "update", {"Status": ["review", "draft"], "Comment": [None, comment.strip()]})
    session.flush()


def clean_post_url(url: str | None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    if not re.match(r"^https://([\w-]+\.)*(linkedin\.com|lnkd\.in|instagram\.com|instagr\.am)/", url, re.IGNORECASE):
        raise ContentError("Paste the link to the LinkedIn or Instagram post.")
    return url[:1000]


def mark_posted(session: Session, post_id: int, user: User, *, url: str | None, posted_on: date | None = None) -> str:
    _require(user)
    post = _get(session, post_id)
    if post.status != "approved":
        raise ContentError("Only approved posts can be marked as posted.")
    post.post_url = clean_post_url(url)
    post.posted_on = posted_on or today_ist()
    post.poster_user_id = post.poster_user_id or user.id
    post.status = "posted"
    complete_tagged_tasks(session, _tag(post.id, "publish"), user)
    metrics_day = post.posted_on + timedelta(days=METRICS_AFTER_DAYS)
    plan_tagged_task(session, tag=_tag(post.id, "metrics"), title=f"Add likes & comments: {post.title}"[:200],
                     day=max(metrics_day, today_ist()), owner_id=post.poster_user_id, user=user,
                     note="Open the post, then paste likes, comments, reposts and impressions on the Content page.")
    _audit(session, user, post.id, "update", {"Status": ["approved", "posted"]})
    session.flush()
    return f"Marked as posted. On {max(metrics_day, today_ist()):%d %b} Today will ask for the likes and comments."


def _count(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(float(str(value).replace(",", "").strip()))
    except ValueError as error:
        raise ContentError("Likes, comments, reposts and impressions must be whole numbers.") from error
    if number < 0:
        raise ContentError("Engagement numbers can't be negative.")
    return number


def save_metrics(session: Session, post_id: int, values: dict, user: User) -> None:
    _require(user)
    post = _get(session, post_id)
    if post.status != "posted":
        raise ContentError("Add engagement once the post is live.")
    for name in METRICS:
        if name in values:
            setattr(post, name, _count(values[name]))
    if all(getattr(post, name) is None for name in METRICS):
        raise ContentError("Enter at least one number.")
    post.metrics_on = today_ist()
    complete_tagged_tasks(session, _tag(post.id, "metrics"), user)
    _audit(session, user, post.id, "update", {"Engagement": [None, ", ".join(
        f"{name} {getattr(post, name)}" for name in METRICS if getattr(post, name) is not None)]})
    session.flush()


def drop_post(session: Session, post_id: int, user: User) -> None:
    _require(user)
    post = _get(session, post_id)
    if post.status == "posted":
        raise ContentError("A live post can't be dropped — delete it on the platform and add a note instead.")
    if user.role != "admin" and post.author_user_id != user.id:
        raise PermissionError("Only the author or an admin can drop a post.")
    post.status = "dropped"
    cancel_tagged_tasks(session, _tag(post.id, "publish"))
    _audit(session, user, post.id, "update", {"Status": [None, "dropped"]})
    session.flush()


def restore_post(session: Session, post_id: int, user: User) -> None:
    _require(user)
    post = _get(session, post_id)
    if post.status != "dropped":
        return
    post.status = "draft" if post.body else "idea"
    _audit(session, user, post.id, "update", {"Status": ["dropped", post.status]})
    session.flush()


# ---------------------------------------------------------------- ideas from news and events


def idea_from_news(session: Session, news_id: int, user: User) -> ContentPost:
    item = session.get(NewsItem, news_id)
    if item is None:
        raise ContentError("That news item is gone.")
    existing = session.scalar(select(ContentPost).where(ContentPost.source_kind == "news",
                                                        ContentPost.source_id == news_id))
    if existing:
        return existing
    theme = "NQM & policy" if re.search(r"\bnqm\b|national quantum mission|dst|policy|scheme", item.title,
                                         re.IGNORECASE) else "Thought leadership"
    return create_post(session, PostData(
        title=item.title[:200], theme=theme, asset_links=item.url, source_kind="news", source_id=news_id,
        body=None), user)


def idea_from_event(session: Session, event_id: int, user: User) -> ContentPost:
    event = session.get(Event, event_id)
    if event is None:
        raise ContentError("That event is gone.")
    existing = session.scalar(select(ContentPost).where(ContentPost.source_kind == "event",
                                                        ContentPost.source_id == event_id,
                                                        ContentPost.status != "dropped"))
    if existing:
        return existing
    upcoming = event.start_on >= today_ist()
    title = f"{'Announce' if upcoming else 'Recap'}: {event.name}"
    planned = event.start_on - timedelta(days=14) if upcoming else today_ist() + timedelta(days=2)
    return create_post(session, PostData(
        title=title[:200], theme="Event", campaign=event.name[:120], planned_on=max(planned, today_ist()),
        asset_links=event.form_url, source_kind="event", source_id=event_id), user)


# ---------------------------------------------------------------- lists and numbers


def engagements(post: ContentPost) -> int | None:
    values = [getattr(post, name) for name in ("likes", "comments", "reposts")]
    return None if all(v is None for v in values) else sum(v or 0 for v in values)


def posts_frame(session: Session, *, statuses: tuple[str, ...] | None = None) -> pd.DataFrame:
    names = dict(session.execute(select(User.id, User.name)).all())
    query = select(ContentPost).order_by(ContentPost.planned_on.is_(None), ContentPost.planned_on.desc(),
                                         ContentPost.id.desc())
    if statuses:
        query = query.where(ContentPost.status.in_(statuses))
    rows = []
    for post in session.scalars(query):
        total = engagements(post)
        rows.append({
            "id": post.id, "Planned": post.planned_on, "Title": post.title,
            "Platform": PLATFORM_LABELS[post.platform], "Theme": post.theme or "", "Campaign": post.campaign or "",
            "Status": STATUS_LABELS[post.status], "Author": names.get(post.author_user_id, ""),
            "Poster": names.get(post.poster_user_id, ""), "Posted": post.posted_on, "Link": post.post_url or "",
            "Likes": post.likes, "Comments": post.comments, "Reposts": post.reposts,
            "Impressions": post.impressions, "Engagements": total,
            "Engagement rate": round(100 * total / post.impressions, 1)
            if total is not None and post.impressions else None,
            "Text": post.body or "", "Hashtags": post.hashtags or "",
        })
    return pd.DataFrame(rows, columns=[
        "id", "Planned", "Title", "Platform", "Theme", "Campaign", "Status", "Author", "Poster", "Posted", "Link",
        "Likes", "Comments", "Reposts", "Impressions", "Engagements", "Engagement rate", "Text", "Hashtags"])


def calendar_events(session: Session, start: date, end: date) -> list[dict]:
    """Posts for the month calendar, coloured by status."""
    colours = {"idea": "#B8A691", "draft": "#7C5CE6", "review": "#2F7F7A", "approved": "#8B5A2B",
               "posted": "#16A34A"}
    events = []
    for post in session.scalars(select(ContentPost).where(
            ContentPost.status != "dropped", ContentPost.planned_on.is_not(None),
            ContentPost.planned_on.between(start, end))):
        day = post.posted_on or post.planned_on
        events.append({"id": str(post.id), "title": f"{PLATFORM_SHORT[post.platform]} · {post.title}",
                       "start": day.isoformat(), "allDay": True, "color": colours[post.status],
                       "extendedProps": {"status": STATUS_LABELS[post.status]}})
    return events


def waiting_approval_count(session: Session) -> int:
    return len(list(session.scalars(select(ContentPost.id).where(ContentPost.status == "review"))))


def theme_performance(session: Session, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Posts with engagement numbers, by theme: which themes do best. Rate = engagements ÷ impressions."""
    query = select(ContentPost).where(ContentPost.status == "posted")
    if start:
        query = query.where(ContentPost.posted_on >= start)
    if end:
        query = query.where(ContentPost.posted_on <= end)
    groups: dict[str, list[ContentPost]] = {}
    for post in session.scalars(query):
        groups.setdefault(post.theme or "No theme", []).append(post)
    rows = []
    for theme, posts in groups.items():
        measured = [p for p in posts if engagements(p) is not None]
        total = sum(engagements(p) for p in measured)
        seen = sum(p.impressions or 0 for p in measured if p.impressions)
        with_seen = sum(engagements(p) for p in measured if p.impressions)
        rows.append({
            "Theme": theme, "Posts": len(posts), "With numbers": len(measured),
            "Likes": sum(p.likes or 0 for p in measured), "Comments": sum(p.comments or 0 for p in measured),
            "Reposts": sum(p.reposts or 0 for p in measured),
            "Avg engagements per post": round(total / len(measured), 1) if measured else None,
            "Engagement rate %": round(100 * with_seen / seen, 1) if seen else None,
        })
    frame = pd.DataFrame(rows, columns=["Theme", "Posts", "With numbers", "Likes", "Comments", "Reposts",
                                        "Avg engagements per post", "Engagement rate %"])
    return frame.sort_values("Avg engagements per post", ascending=False, na_position="last").reset_index(drop=True)


def content_week(session: Session, start: date, end: date) -> dict:
    """For the weekly report: posts that went out this week, the queue, and the best themes so far."""
    posted = list(session.scalars(select(ContentPost).where(
        ContentPost.status == "posted", ContentPost.posted_on.between(start, end))))
    upcoming = list(session.scalars(select(ContentPost.id).where(
        ContentPost.status == "approved", ContentPost.planned_on.between(end + timedelta(days=1),
                                                                         end + timedelta(days=7)))))
    measured = [p for p in session.scalars(select(ContentPost).where(
        ContentPost.status == "posted", ContentPost.metrics_on.is_not(None))) if engagements(p) is not None]
    best = max(measured, key=lambda p: engagements(p), default=None)
    return {
        "posted": len(posted),
        "linkedin": sum(1 for p in posted if p.platform in ("linkedin", "both")),
        "instagram": sum(1 for p in posted if p.platform in ("instagram", "both")),
        "waiting_approval": waiting_approval_count(session),
        "approved_next_week": len(upcoming),
        "best_post": (best.title, engagements(best), best.theme or "No theme") if best else None,
        "themes": theme_performance(session, start - timedelta(days=83), end),   # last 12 weeks
    }


def status_choices() -> list[str]:
    return list(POST_STATUSES)
