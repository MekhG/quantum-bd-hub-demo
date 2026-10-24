"""Content calendar: LinkedIn / Instagram posts from idea to draft, approval, posting and engagement.

The hub doesn't publish to LinkedIn or Instagram itself (that needs the platforms' developer approval); the
approved text is posted by hand or with the platform's own scheduler, then marked as posted with its link.
A week later the likes, comments, reposts and impressions are pasted in, so themes can be compared.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin
from app.models.core import User

POST_PLATFORMS = ("linkedin", "instagram", "both")
POST_STATUSES = ("idea", "draft", "review", "approved", "posted", "dropped")


class ContentPost(TimestampMixin, Base):
    __tablename__ = "content_posts"
    __table_args__ = (
        CheckConstraint(f"platform IN {POST_PLATFORMS}", name="platform_valid"),
        CheckConstraint(f"status IN {POST_STATUSES}", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(10), default="linkedin", server_default="linkedin")
    theme: Mapped[str | None] = mapped_column(String(60))           # option list 'content_theme'
    campaign: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="idea", server_default="idea", index=True)
    planned_on: Mapped[date | None] = mapped_column(Date, index=True)
    body: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[str | None] = mapped_column(String(500))
    document_ids: Mapped[list[int] | None] = mapped_column(JSONType)   # images/videos from the Library
    asset_links: Mapped[str | None] = mapped_column(Text)             # other links, one per line
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    poster_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_on: Mapped[date | None] = mapped_column(Date)
    review_comment: Mapped[str | None] = mapped_column(Text)
    posted_on: Mapped[date | None] = mapped_column(Date)
    post_url: Mapped[str | None] = mapped_column(String(1000))
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    reposts: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    metrics_on: Mapped[date | None] = mapped_column(Date)
    # Where the idea came from: 'news' / 'event' and that item's id.
    source_kind: Mapped[str | None] = mapped_column(String(20))
    source_id: Mapped[int | None] = mapped_column(Integer)

    author: Mapped[User | None] = relationship(foreign_keys=[author_user_id])
    poster: Mapped[User | None] = relationship(foreign_keys=[poster_user_id])
