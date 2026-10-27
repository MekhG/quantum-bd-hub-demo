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
        st.Page("views/colleges.py", title="Colleges", icon=":material/account_balance:"),
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



@st.cache_resource(show_spinner="Preparing today's demo data — about 20 seconds, once a day…")
def _demo_data_ready(day: str) -> str:
    """Once per day per server: rebuild the fictional data so it always ends today (and visitors' edits reset)."""
    from app.db import reset_engine
    from app.demo.refresh import ensure_fresh

    if ensure_fresh(on_swap=reset_engine):
        st.cache_data.clear()
    return day


def _has_visitor() -> bool:
    # The welcome page (no persona picked yet) shows no menu: there is nothing to open until you pick one.
    if not is_demo():
        return True
    from app.demo.personas import chosen_email

    return chosen_email() is not None


current = st.navigation(SECTIONS, position="top" if _has_visitor() else "hidden")
st.session_state["_qa_page_icon"] = current.icon
st.set_page_config(page_title=f"{current.title} · Demo BD Hub")
if is_demo():
    from app.demo.refresh import today_ist

    _demo_data_ready(today_ist().isoformat())
user = authenticate()
if is_demo() and user.role == "viewer" and current.title == "Today" and not st.session_state.get("viewer_landed"):
    # Leadership has no tasks: their first page is the Dashboard.
    st.session_state["viewer_landed"] = True
    st.switch_page(SECTIONS["Overview"][2])
counts = load_shell_counts(user.id, user.name)
if is_demo():
    st.html('<div class="qa-demo-banner"><b>Live demo</b> · Demo is a fictional company — every institution, person '
            'and number is made up. The data rebuilds daily, so try anything.</div>')
render_shell(user, counts)
show_edit_flash()  # result of the last save / log, shown at the top of whichever page is open
current.run()
