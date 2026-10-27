"""Shared look: the top menu, the command bar (search, + New, alerts, who you are), the "needs attention" strip,
banner page headers, empty states, stage colours.

Colours and fonts come from .streamlit/config.toml; the few custom pieces (chips, initials, cards) come from
app/ui/components.py so every page looks the same.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.config import is_demo
from app.services.auth import auth_configured
from app.services.notifications import ShellCounts, shell_counts
from app.services.search import MIN_CHARS, Hit, search_everything
from app.ui.common import CurrentUser
from app.ui.components import avatar, chip, esc, html_block, inject_css

ASSETS = Path(__file__).resolve().parents[2] / "assets"
LOGO = ASSETS / "demo_logo.png"
MARK = ASSETS / "demo_mark.png"
FAVICON = ASSETS / "favicon.png"

_ROLE_LABEL = {"admin": "Admin", "member": "Member", "viewer": "Viewer"}

# Stage pill colours follow the stage's outcome (a config value), so renaming or adding stages needs no code.
_OUTCOME_COLOUR = {"open": "blue", "won": "green", "nurture": "orange", "lost": "red"}

_KIND_ICON = {"lead": ":material/target:", "contact": ":material/person:", "college": ":material/account_balance:"}
_ALERT_ICON = {"handoff": ":material/forward:", "query": ":material/schedule:", "ghost": ":material/help:"}


@st.cache_data(ttl=60, show_spinner=False)
def load_shell_counts(user_id: int, user_name: str) -> ShellCounts:
    """Badges and alerts. Cached briefly; any save clears the cache (st.cache_data.clear) so they refresh."""
    with session_scope() as session:
        return shell_counts(session, user_id, user_name)


def _attention(counts: ShellCounts) -> list[tuple[str, str, str, dict | None]]:
    """What needs a look now, as (page, label, icon, query params). Shown as a strip of chips under the command bar,
    so the counts are visible from every page (text, never colour alone)."""
    items: list[tuple[str, str, str, dict | None]] = []
    if counts.due_now:
        late = f", {counts.overdue} overdue" if counts.overdue else ""
        items.append(("views/today.py", f"{counts.due_now} due today{late}", ":material/today:", None))
    if counts.late_queries:
        items.append(("views/queries.py", f"{counts.late_queries} late {'query' if counts.late_queries == 1 else 'queries'}",
                      ":material/forum:", None))
    if counts.lead_news:
        items.append(("views/news.py", f"{counts.lead_news} news about leads", ":material/newspaper:", None))
    if counts.registrations:
        items.append(("views/partners.py", f"{counts.registrations} deal registration to decide"
                      if counts.registrations == 1 else f"{counts.registrations} deal registrations to decide",
                      ":material/handshake:", None))
    if counts.posts_waiting:
        items.append(("views/content.py", f"{counts.posts_waiting} post{'s' if counts.posts_waiting != 1 else ''} "
                      "to approve", ":material/campaign:", None))
    if counts.privacy_open:
        text = (f"{counts.privacy_overdue} privacy request{'s' if counts.privacy_overdue != 1 else ''} overdue"
                if counts.privacy_overdue else
                f"{counts.privacy_open} privacy request{'s' if counts.privacy_open != 1 else ''} open")
        items.append(("views/privacy.py", text, ":material/shield_person:", None))
    return items


def _person_menu(user: CurrentUser) -> None:
    """Who you are, top right: a pill with your initials; inside, your role and the persona switch (or sign out)."""
    with st.popover(user.first_name or user.name, icon=":material/account_circle:", key="qa-person"):
        html_block(f'<div class="qa-user">{avatar(user.name, 40)}<div><b>{esc(user.name)}</b>'
                   f'{chip(_ROLE_LABEL.get(user.role, user.role.title()), "blue")}</div></div>')
        if is_demo():
            from app.demo.personas import switch_persona

            st.caption("Try the hub as someone else — the menus and buttons change with the role.")
            st.button("Switch persona", on_click=switch_persona, icon=":material/switch_account:", width="stretch",
                      key="switch_persona")
        elif auth_configured():
            st.button("Sign out", on_click=st.logout, icon=":material/logout:", width="stretch", key="sign_out")


def render_shell(user: CurrentUser, counts: ShellCounts) -> None:
    """Everything above the page: the logo in the top menu, the command bar and the attention strip."""
    inject_css()
    st.logo(str(LOGO), size="large", icon_image=str(MARK))
    with st.container(key="qa-topbar"):
        left, right = st.columns([3.0, 2.0], vertical_alignment="center")
        with left:
            _search_box()
        with right, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            _alerts(counts)
            _new_menu(user)
            _person_menu(user)
    items = _attention(counts)
    if items:
        with st.container(horizontal=True, gap="small", key="qa-attention"):
            for page, label, icon_name, params in items:
                st.page_link(page, label=label, icon=icon_name, query_params=params)


@st.cache_data(ttl=30, show_spinner=False)
def _search(text: str) -> tuple[list[Hit], list[tuple[str, str, str]]]:
    from app.services.library import search_library

    with session_scope() as session:
        return search_everything(session, text), search_library(session, text)


@st.fragment
def _search_box() -> None:
    # A fragment, so typing reruns only the search results, not the whole page underneath.
    text = st.text_input("Search the hub", key="global_q", type="search", live="400ms",
                         placeholder="Search leads, contacts, colleges or AISHE code…",
                         label_visibility="collapsed", icon=":material/search:")
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return
    hits, library = _search(text)
    with st.container(border=True, key="qa-card-plain-search"):
        if not hits and not library:
            st.caption(f"Nothing matches “{text}”. Try part of a name, an email or an AISHE code.")
            return
        for kind, heading in (("lead", "Leads"), ("contact", "Contacts"), ("college", "Colleges (not yet leads)")):
            group = [h for h in hits if h.kind == kind]
            if not group:
                continue
            html_block(f'<div class="qa-eyebrow" style="margin-top:0.2rem">{heading}</div>')
            for hit in group:
                label = f"**{hit.title}**" + (f" — {hit.detail}" if hit.detail else "")
                if hit.lead_id:
                    st.page_link("views/leads.py", label=label, icon=_KIND_ICON[kind],
                                 query_params={"lead": hit.lead_id}, width="stretch")
                elif kind == "contact":
                    st.page_link("views/contacts.py", label=label, icon=_KIND_ICON[kind],
                                 query_params={"q": hit.query or hit.title}, width="stretch")
                else:
                    st.page_link("views/colleges.py", label=label, icon=_KIND_ICON[kind],
                                 query_params={"q": hit.query or hit.title}, width="stretch")
        if library:
            html_block('<div class="qa-eyebrow" style="margin-top:0.2rem">Library</div>')
            for kind, title, extra in library:
                if kind == "document":
                    st.link_button(f"{title}", extra, icon=":material/description:", type="tertiary")
                else:
                    st.page_link("views/library.py", label=f"**{title}** — {extra} script", icon=":material/article:",
                                 width="stretch")


def _new_menu(user: CurrentUser) -> None:
    from app.ui.quick_log import quick_log_dialog  # imported here to keep start-up light
    from app.ui.task_dialog import new_task_dialog

    with st.popover("New", icon=":material/add:", type="primary", disabled=not user.can("member")):
        if st.button("Log activity", icon=":material/add_call:", width="stretch", key="new_log",
                     help="A call, WhatsApp, meeting or demo that already happened (or is planned)."):
            quick_log_dialog(user)
        if st.button("Task", icon=":material/add_task:", width="stretch", key="new_task"):
            new_task_dialog(user)
        st.page_link("views/queries.py", label="Query", icon=":material/add_comment:", query_params={"new": "query"},
                     width="stretch")
        st.page_link("views/contacts.py", label="Contact", icon=":material/person_add:",
                     query_params={"new": "contact"}, width="stretch")
        st.page_link("views/import_data.py", label="Leads from a sheet or AI table", icon=":material/upload_file:",
                     width="stretch")


def _alerts(counts: ShellCounts) -> None:
    n = len(counts.alerts)
    with st.popover(f"{n} alert{'s' if n != 1 else ''}" if n else "Alerts", icon=":material/notifications:",
                    help="Hand-offs, late queries and finished cadences"):
        if not n:
            st.caption("You're all caught up.")
            return
        for alert in counts.alerts:
            with st.container(gap=None):
                if alert.lead_id:
                    st.page_link("views/leads.py", label=f"**{alert.title}**", icon=_ALERT_ICON[alert.kind],
                                 query_params={"lead": alert.lead_id}, width="stretch")
                else:
                    st.page_link("views/today.py", label=f"**{alert.title}**", icon=_ALERT_ICON[alert.kind],
                                 width="stretch")
                st.caption(alert.detail)


def page_header(title: str, subtitle: str | None = None, eyebrow: str | None = None) -> None:
    """A banner at the top of each page: the page's icon in a round badge, where you are, the title and one line on
    what the page is for."""
    symbol = st.session_state.get("_qa_page_icon") or ""
    symbol = symbol.removeprefix(":material/").removesuffix(":")
    badge = (f'<div class="qa-hero-badge" aria-hidden="true"><span class="qa-ms">{esc(symbol)}</span></div>'
             if symbol else "")
    crumb = f'<div class="qa-hero-crumb">{esc(eyebrow)}</div>' if eyebrow else ""
    sub = f'<p class="qa-hero-sub">{esc(subtitle)}</p>' if subtitle else ""
    html_block(f'<div class="qa-hero">{badge}<div class="qa-hero-text">{crumb}<h1 class="qa-hero-title">'
               f'{esc(title)}</h1>{sub}</div></div>')


def section_header(title: str, caption: str | None = None) -> None:
    st.subheader(title, anchor=False)
    if caption:
        st.caption(caption)


def empty_state(message: str, *, page: str | None = None, label: str | None = None,
                icon: str = ":material/inbox:") -> None:
    with st.container(border=True):
        st.markdown(f"**{message}**")
        if page and label:
            st.page_link(page, label=label, icon=icon)


def stage_pills(frame: pd.DataFrame, stages: pd.DataFrame) -> tuple[pd.DataFrame, st.column_config.Column]:
    """Turns the Stage column into a coloured pill for display.

    `stages` needs columns name, outcome and sort_order. Returns the display copy and its column config.
    """
    ordered = stages.sort_values("sort_order")
    names = ordered["name"].tolist()
    colours = []
    first_open = True
    for outcome in ordered["outcome"]:
        if outcome == "open" and first_open:
            colours.append("gray")  # the very first stage ('New') = not worked yet
            first_open = False
        else:
            colours.append(_OUTCOME_COLOUR.get(outcome, "gray"))
    shown = frame.copy()
    shown["Stage"] = shown["Stage"].map(lambda s: [s] if isinstance(s, str) and s else [])
    config = st.column_config.MultiselectColumn("Stage", options=names, color=colours, disabled=True)
    return shown, config


def as_list_column(frame: pd.DataFrame, column: str, sep: str = ", ") -> pd.DataFrame:
    """'A, B, C' → ['A', 'B', 'C'] so the table shows each item as a chip."""
    shown = frame.copy()
    shown[column] = shown[column].fillna("").map(lambda text: [t for t in str(text).split(sep) if t])
    return shown


def blank_missing_text(frame: pd.DataFrame) -> pd.DataFrame:
    """Shows empty cells instead of 'None' in text columns (numbers, dates and lists are left alone)."""
    shown = frame.copy()
    for column in shown.columns:
        series = shown[column]
        if pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series):
            if not series.map(lambda v: isinstance(v, (list, bool))).any():
                shown[column] = series.fillna("")
    return shown


def time_options(step_minutes: int = 15) -> list[str]:
    """'07:00' … '21:45' — a plain list is easier on a phone than a clock picker."""
    return [f"{h:02d}:{m:02d}" for h in range(7, 22) for m in range(0, 60, step_minutes)]


def nearest_time(value, step_minutes: int = 15) -> str:
    """Rounds a time to the list above (keeps out-of-hours times by snapping to the nearest end)."""
    minutes = value.hour * 60 + value.minute
    minutes = min(max(minutes, 7 * 60), 21 * 60 + 45)
    minutes = int(round(minutes / step_minutes) * step_minutes)
    minutes = min(minutes, 21 * 60 + 45)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
