"""Quantum BD & Marketing Hub — portfolio demo entry point.

Run locally with:  streamlit run streamlit_app.py
This file makes sure today's demo data exists, lets the visitor pick a persona, draws the top menu and command bar, then runs
the chosen page from views/.
"""

from __future__ import annotations

import os


def _drop_unusable_ssl_keylog() -> None:
    # Some Windows security/VPN tools set SSLKEYLOGFILE to a file Python may not open. Python's ssl module then
    # crashes whenever any library sets up HTTPS (e.g. the chart library on Home), so ignore it in that case.
    path = os.environ.get("SSLKEYLOGFILE")
    if not path:
        return
    try:
        with open(path, "a"):
            pass
    except OSError:
        os.environ.pop("SSLKEYLOGFILE", None)


_drop_unusable_ssl_keylog()

import streamlit as st  # noqa: E402  (must come after the fix above)

from app.config import is_demo  # noqa: E402
from app.ui.common import authenticate  # noqa: E402
from app.ui.edit_table import show_edit_flash  # noqa: E402
from app.ui.layout import FAVICON, load_shell_counts, render_shell  # noqa: E402

st.set_page_config(page_title="Demo BD Hub", page_icon=str(FAVICON), layout="wide",
                   initial_sidebar_state="collapsed")  # the menu is at the top; there is no sidebar

# Menu sections.
SECTIONS: dict[str, list[st.Page]] = {
    "Overview": [
        st.Page("views/today.py", title="Today", icon=":material/today:", default=True),
        st.Page("views/calendar.py", title="Calendar", icon=":material/calendar_month:"),
        st.Page("views/home.py", title="Dashboard", icon=":material/space_dashboard:", url_path="dashboard"),
        st.Page("views/report.py", title="Weekly report", icon=":material/summarize:", url_path="report"),
    ],
    "Pipeline": [
        st.Page("views/queries.py", title="Queries", icon=":material/forum:"),
        st.Page("views/leads.py", title="Leads", icon=":material/target:"),
        st.Page("views/quotes.py", title="Quotes", icon=":material/request_quote:"),
        st.Page("views/contacts.py", title="Contacts", icon=":material/contacts:"),
        st.Page("views/organisations.py", title="Organisations", icon=":material/domain:"),
        st.Page("views/partners.py", title="Partners", icon=":material/handshake:"),
        st.Page("views/clients.py", title="Clients", icon=":material/verified:"),
    ],
    "Knowledge": [
        st.Page("views/library.py", title="Library", icon=":material/library_books:"),
        st.Page("views/news.py", title="News & signals", icon=":material/newspaper:", url_path="news"),
    ],
    "Marketing": [
        st.Page("views/events.py", title="Events", icon=":material/event:"),
        st.Page("views/community.py", title="Community", icon=":material/groups:"),
        st.Page("views/content.py", title="Content", icon=":material/campaign:"),
        st.Page("views/gtm.py", title="GTM charts", icon=":material/account_tree:", url_path="gtm"),
    ],
    "Data": [
        st.Page("views/import_data.py", title="Import", icon=":material/upload_file:", url_path="import"),
        st.Page("views/history.py", title="History", icon=":material/history:"),
        st.Page("views/privacy.py", title="Privacy requests", icon=":material/shield_person:", url_path="privacy"),
        st.Page("views/configuration.py", title="Configuration", icon=":material/tune:"),
    ],
    "Help": [
        st.Page("views/guide.py", title="Guide", icon=":material/help:"),
        st.Page("views/about.py", title="About this demo", icon=":material/info:", url_path="about"),
    ],
}



def _start_demo_data() -> None:
    """Never waits: builds today's fictional data in the background if it isn't there yet. Visitors keep using
    yesterday's until it's swapped in; the data rebuilds daily so it always ends today and edits reset."""
    from app.db import reset_engine
    from app.demo.refresh import start_refresh

    start_refresh(on_swap=reset_engine)


def _wait_for_first_data() -> None:
    """Only on a brand-new server (nothing to show yet): a progress bar until the first build is done."""
    from app.demo import refresh

    if refresh.has_data():
        return
    from app.ui.components import inject_css
    from app.ui.layout import page_header

    inject_css()
    st.session_state["_qa_page_icon"] = ":material/hourglass_top:"
    holder = st.empty()
    with holder.container():
        page_header("Preparing the demo data",
                    "The app has just woken up, so it is writing twelve weeks of fictional BD work. This happens "
                    "once; after that every page opens straight away.", eyebrow="One moment")
        bar = st.progress(0.0)
        while refresh.is_building() and not refresh.has_data():
            elapsed = refresh.seconds_building()
            done = min(elapsed / refresh.TYPICAL_BUILD_SECONDS, 0.95)
            left = max(int(refresh.TYPICAL_BUILD_SECONDS - elapsed), 1)
            bar.progress(done, text=f"About {left} seconds left…" if done < 0.95 else "Almost there…")
            refresh.wait_for_data(0.5)
    if not refresh.has_data():
        st.error("The demo data couldn't be built. Reload the page to try again.")
        if refresh.build_error():
            with st.expander("Details"):
                st.code(refresh.build_error())
        st.stop()
    holder.empty()
    st.rerun()


@st.cache_resource
def _seen_generation() -> dict[str, int]:
    return {"value": 0}


def _clear_caches_after_saves() -> None:
    """Pages cache their tables for up to an hour; any save (or a fresh day's data) clears every cache once."""
    from app.db import data_generation

    seen, now = _seen_generation(), data_generation()
    if seen["value"] != now:
        st.cache_data.clear()
        seen["value"] = now


def _has_visitor() -> bool:
    # The welcome page (no persona picked yet) shows no menu: there is nothing to open until you pick one.
    if not is_demo():
        return True
    from app.demo.personas import chosen_email

    return chosen_email() is not None


if is_demo():
    _start_demo_data()  # first thing on every run, so a sleeping app starts building while the welcome page shows
current = st.navigation(SECTIONS, position="top" if _has_visitor() else "hidden")
st.session_state["_qa_page_icon"] = current.icon
st.set_page_config(page_title=f"{current.title} · Demo BD Hub")
if is_demo() and _has_visitor():
    _wait_for_first_data()
_clear_caches_after_saves()
user = authenticate()
if is_demo() and user.role == "viewer" and current.title == "Today" and not st.session_state.get("viewer_landed"):
    # Leadership has no tasks: their first page is the Dashboard.
    st.session_state["viewer_landed"] = True
    st.switch_page(SECTIONS["Overview"][2])
counts = load_shell_counts(user.id, user.name)
if is_demo():
    from app.demo.refresh import is_building, is_fresh

    note = (" Today's data is on its way — you're seeing yesterday's for a minute." if is_building() and not is_fresh()
            else " The data rebuilds daily, so try anything.")
    st.html('<div class="qa-demo-banner"><b>Live demo</b> · Demo is a fictional company — every organisation, '
            f'person and number is made up.{note}</div>')
render_shell(user, counts)
show_edit_flash()  # result of the last save / log, shown at the top of whichever page is open
current.run()
