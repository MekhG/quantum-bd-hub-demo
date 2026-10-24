"""Shared look: the sidebar menu, the top bar (search, + New, alerts), page headers, empty states, stage colours.

Colours and fonts come from .streamlit/config.toml; the few custom pieces (chips, initials, cards) come from
app/ui/components.py so every page looks the same.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


@st.cache_resource
def _logo_b64() -> str:
    import base64

    return base64.b64encode(LOGO.read_bytes()).decode()


@st.cache_data(ttl=60, show_spinner=False)
def load_shell_counts(user_id: int, user_name: str) -> ShellCounts:
    """Badges and alerts. Cached briefly; any save clears the cache (st.cache_data.clear) so they refresh."""
    with session_scope() as session:
        return shell_counts(session, user_id, user_name)


def _badge_label(title: str, counts: ShellCounts) -> str:
    # Counts ride in the label as Streamlit badges (text + colour, never colour alone).
    if title == "Today" and counts.due_now:
        tone = "orange" if counts.overdue else "blue"
        return f"{title} :{tone}-badge[{counts.due_now}]"
    if title == "Queries" and counts.late_queries:
        return f"{title} :orange-badge[{counts.late_queries} late]"
    if title == "News & signals" and counts.lead_news:
        return f"{title} :blue-badge[{counts.lead_news}]"
    if title == "Partners" and counts.registrations:
        return f"{title} :orange-badge[{counts.registrations} to decide]"
    if title == "Content" and counts.posts_waiting:
        return f"{title} :orange-badge[{counts.posts_waiting} to approve]"
    if title == "Privacy requests" and counts.privacy_open:
        return (f"{title} :orange-badge[{counts.privacy_overdue} overdue]" if counts.privacy_overdue
                else f"{title} :blue-badge[{counts.privacy_open}]")
    return title


def render_sidebar(sections: Mapping[str, Sequence[st.Page]], user: CurrentUser, counts: ShellCounts) -> None:
    inject_css()
    with st.sidebar:
        st.markdown(f'<img src="data:image/png;base64,{_logo_b64()}" alt="Demo" width="150" '
                    'style="margin-bottom:0.15rem">', unsafe_allow_html=True)
        st.caption("**BD & Marketing Hub**")
        for section, pages in sections.items():
            html_block(f'<div class="qa-eyebrow" style="margin:0.6rem 0 -0.35rem 0.55rem">{esc(section)}</div>')
            for page in pages:
                st.page_link(page, label=_badge_label(page.title, counts), icon=page.icon, width="stretch")
        st.space("medium")
        with st.container(border=True):
            html_block(f'<div class="qa-user">{avatar(user.name, 36, colour="#7A4A26")}<div><b>{esc(user.name)}</b>'
                       f'{chip(_ROLE_LABEL.get(user.role, user.role.title()), "blue")}</div></div>')
            if is_demo():
                from app.demo.personas import switch_persona

                st.button("Switch persona", on_click=switch_persona, icon=":material/switch_account:",
                          type="tertiary", key="switch_persona")
            elif auth_configured():
                st.button("Sign out", on_click=st.logout, icon=":material/logout:", type="tertiary",
                          key="sign_out")


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


def top_bar(user: CurrentUser, counts: ShellCounts) -> None:
    with st.container(key="qa-topbar"):
        left, right = st.columns([3.2, 1.6], vertical_alignment="top")
        with left:
            _search_box()
        with right, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            _alerts(counts)
            _new_menu(user)


def page_header(title: str, subtitle: str | None = None, eyebrow: str | None = None) -> None:
    if eyebrow:
        html_block(f'<div class="qa-eyebrow">{esc(eyebrow)}</div>')
    st.title(title, anchor=False)
    if subtitle:
        st.caption(subtitle)


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
