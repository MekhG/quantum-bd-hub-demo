"""Configuration → News & signals: the news searches and feeds, the signal types (points, fading, headline words)
and the settings (refresh interval, hot score)."""

from __future__ import annotations

import streamlit as st

from app.db import session_scope
from app.models import User
from app.services.news import NewsError, rematch, save_sources, sources_frame
from app.services.procurement import get_settings, save_settings
from app.services.signals import SignalError, recompute_scores, save_types, types_frame
from app.ui.common import CurrentUser

FLASH = "edit_flash"


def _done(message: str) -> None:
    st.session_state[FLASH] = ("success", message, [])
    st.cache_data.clear()
    st.rerun()


def _sources(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        frame = sources_frame(session)
    st.caption("Free sources only. A Google News search takes search words like you'd type them in Google News: "
               "quotes for an exact phrase, OR between alternatives. An RSS feed takes the feed's address. "
               "“Only keep if it mentions” drops items that don't contain any of those words (comma-separated) — "
               "useful for broad feeds such as PIB. Delete a row to remove the source.")
    edited = st.data_editor(
        frame, key="news_sources_editor", hide_index=True, width="stretch",
        num_rows="dynamic" if admin else "fixed",
        disabled=not admin or ["id", "Last checked", "New last time", "Problem"],
        column_order=["Name", "Kind", "Search words", "RSS address", "Topic", "Only keep if it mentions", "Active",
                      "Last checked", "New last time", "Problem"],
        column_config={
            "Name": st.column_config.TextColumn(required=True, width="medium"),
            "Kind": st.column_config.SelectboxColumn(options=["Google News search", "RSS feed"], required=True,
                                                     default="Google News search"),
            "Search words": st.column_config.TextColumn(width="large"),
            "RSS address": st.column_config.TextColumn(width="medium"),
            "Topic": st.column_config.TextColumn(required=True, help="News is filtered by topic on the News page."),
            "Active": st.column_config.CheckboxColumn(default=True, width="small"),
            "Last checked": st.column_config.DatetimeColumn(format="DD MMM, HH:mm"),
            "New last time": st.column_config.NumberColumn(format="%d", width="small"),
            "Problem": st.column_config.TextColumn(width="medium"),
        })
    if admin and st.button("Save news sources", type="primary", icon=":material/save:", key="news_sources_save"):
        try:
            with session_scope() as session:
                n = save_sources(session, edited, session.get(User, user.id))
        except (NewsError, PermissionError) as error:
            st.error(str(error))
        else:
            _done(f"News sources saved ({n} change(s)). Changed searches are fetched on the next visit to News.")


def _types(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        frame = types_frame(session)
    st.caption("Points add up to the lead score and fade in a straight line to zero over the months set here. "
               "Headline words suggest a signal type for a news item (comma-separated; end a word with * to match "
               "its start, e.g. inaugurat*). Automatic types are worked out by the hub — you can change their "
               "points, not their names.")
    edited = st.data_editor(
        frame, key="signal_types_editor", hide_index=True, width="stretch",
        num_rows="dynamic" if admin else "fixed", disabled=not admin or ["id", "Automatic"],
        column_order=["Signal", "Points", "Fades over (months)", "Headline words", "What it means", "Automatic",
                      "Active"],
        column_config={
            "Signal": st.column_config.TextColumn(required=True, width="medium"),
            "Points": st.column_config.NumberColumn(min_value=0, max_value=100, step=1, required=True, width="small"),
            "Fades over (months)": st.column_config.NumberColumn(min_value=1, max_value=60, step=1, required=True,
                                                                 default=12, width="small"),
            "Headline words": st.column_config.TextColumn(width="large"),
            "What it means": st.column_config.TextColumn(width="large"),
            "Automatic": st.column_config.CheckboxColumn(width="small"),
            "Active": st.column_config.CheckboxColumn(default=True, width="small"),
        })
    if admin and st.button("Save signal types", type="primary", icon=":material/save:", key="signal_types_save"):
        try:
            with session_scope() as session:
                n = save_types(session, edited, session.get(User, user.id))
                rescored = recompute_scores(session) if n else 0
                rematch(session)
        except (SignalError, PermissionError) as error:
            st.error(str(error))
        else:
            _done(f"Signal types saved ({n} change(s)); {rescored} lead score(s) changed.")


def _settings(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        settings = get_settings(session)

    def number(key: str, default: int) -> int:
        try:
            return int(settings.get(key) or default)
        except ValueError:
            return default

    with st.form("intel_settings", border=False):
        c1, c2, c3 = st.columns(3)
        hot = c1.number_input("Hot lead score", min_value=5, max_value=200, step=5, value=number("hot_lead_score", 40),
                              disabled=not admin,
                              help="Leads at or above this score are marked hot on Today, Leads and the Dashboard.")
        hours = c2.number_input("Check the news every (hours)", min_value=1, max_value=48, step=1,
                                value=number("news_refresh_hours", 6), disabled=not admin,
                                help="Feeds are read when someone opens News & signals and the last check is older "
                                     "than this.")
        keep = c3.number_input("Keep unread news for (days)", min_value=14, max_value=365, step=7,
                               value=number("news_keep_days", 120), disabled=not admin,
                               help="Older news nobody acted on is cleared. Saved items and signals are kept.")
        if st.form_submit_button("Save settings", type="primary", icon=":material/save:", disabled=not admin):
            with session_scope() as session:
                n = save_settings(session, {"hot_lead_score": str(hot), "news_refresh_hours": str(hours),
                                            "news_keep_days": str(keep)}, session.get(User, user.id))
            _done(f"Settings saved ({n} change(s)).")


def render_intel_setup(user: CurrentUser) -> None:
    tabs = st.tabs(["News sources", "Signal types", "Settings"])
    for tab, render in zip(tabs, (_sources, _types, _settings)):
        with tab:
            render(user)
    if not user.can("admin"):
        st.caption("Only an admin can change these.")
