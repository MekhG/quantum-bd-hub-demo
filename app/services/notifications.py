"""What the attention strip and the bell show: counts and in-app alerts for one person."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from app.services.activity_log import ghost_frame, task_frame
from app.services.queries import queries_frame

HANDOFF_MARK = "Handed off by"


@dataclass
class Alert:
    kind: str          # "handoff" / "query" / "ghost"
    title: str
    detail: str
    lead_id: int | None = None


@dataclass
class ShellCounts:
    due_now: int = 0            # overdue + due today, mine
    overdue: int = 0
    late_queries: int = 0       # everyone's: shown on the Queries menu item
    lead_news: int = 0          # unread news naming a lead institution: shown on News & signals
    registrations: int = 0      # partner deal registrations waiting for an admin decision
    posts_waiting: int = 0      # content posts waiting for approval
    privacy_open: int = 0       # DPDP requests not yet closed
    privacy_overdue: int = 0
    alerts: list[Alert] = field(default_factory=list)


def _lead(value) -> int | None:
    return None if value is None or pd.isna(value) else int(value)


def shell_counts(session: Session, user_id: int, user_name: str) -> ShellCounts:
    tasks = task_frame(session, owner_id=user_id)
    now = tasks[tasks["Bucket"].isin(["Overdue", "Due"])]
    counts = ShellCounts(due_now=len(now), overdue=int((tasks["Bucket"] == "Overdue").sum()))

    # Tasks a teammate handed to me or set for me (not ones the cadence planned for me).
    passed = tasks[(tasks["From"] != "") | tasks["Note"].str.contains(HANDOFF_MARK, regex=False)]
    for row in passed.head(8).to_dict("records"):
        who = row["From"] or row["Note"].split(HANDOFF_MARK, 1)[-1].split(" to ", 1)[0].strip()
        where = f" · {row['Organisation']}" if row["Organisation"] else ""
        counts.alerts.append(Alert("handoff", f"{row['Task']}{where}",
                                   f"From {who} · due {row['Due'].strftime('%d %b')}", _lead(row["lead_id"])))

    queries = queries_frame(session, days=30)
    if not queries.empty:
        late = queries[queries["Status"] == "Reply overdue"]
        counts.late_queries = len(late)
        for row in late[late["Owner"] == user_name].head(5).to_dict("records"):
            counts.alerts.append(Alert("query", f"Reply overdue · {row['From'] or row['Organisation']}",
                                       f"{row['Channel']} · received {row['Received'].strftime('%d %b, %H:%M')}",
                                       _lead(row["lead_id"])))

    from app.services.news import unread_lead_news  # local: keeps feedparser off the startup path

    counts.lead_news = unread_lead_news(session)

    from app.services.content import waiting_approval_count
    from app.services.partners import pending_count
    from app.services.privacy_requests import open_counts

    counts.registrations = pending_count(session)
    counts.posts_waiting = waiting_approval_count(session)
    counts.privacy_open, counts.privacy_overdue = open_counts(session)

    ghosts = ghost_frame(session, owner_id=user_id)
    if not ghosts.empty:
        counts.alerts.append(Alert("ghost", f"{len(ghosts)} cadence(s) finished with no reply",
                                   "Decide on Today: mark Ghosted or keep open"))
    return counts
