"""About this demo: what it is, how it's built, and what's real (the code) versus made up (the data)."""

from __future__ import annotations

import streamlit as st

from app.config import get_setting
from app.ui.common import require_user
from app.ui.layout import page_header

require_user()
page_header("About this demo", "What you're looking at, and how it's built.", eyebrow="Help")

author = get_setting("DEMO_AUTHOR", "Mekhala Ganguly")
author_url = get_setting("DEMO_AUTHOR_URL")
repo_url = get_setting("DEMO_REPO_URL")

st.markdown(
    f"This is a working copy of an internal business-development hub built by **{author}** for a deep-tech company "
    "that sells quantum teaching labs (simulators and benchtop experimentation kits) to universities in India — "
    "and quantum upskilling to companies and public bodies. "
    "The code and every feature are the real thing; the company **Demo**, its team, organisations, contacts and prices "
    "are fictional." + (f" More projects: [portfolio]({author_url})." if author_url else ""))

left, right = st.columns(2)
with left, st.container(border=True):
    st.markdown("**The problem**")
    st.markdown(
        "- Leads, follow-ups, scripts, brochures and reports lived in separate spreadsheets, inboxes and folders.\n"
        "- Nobody could see *who to contact today* or *what was said last* across email, calls, WhatsApp and "
        "meetings.\n"
        "- The weekly leadership report took hours to assemble by hand.")
with right, st.container(border=True):
    st.markdown("**What the hub does**")
    st.markdown(
        "- One CRM for any organisation — universities, companies, schools, labs, public bodies — filtered by "
        "industry, with contacts and leads across eight lead tracks and safe spreadsheet imports.\n"
        "- **Cadences** plan every follow-up; a reply stops them; Today shows each person's work.\n"
        "- Quotes as Word files, partner deal registration, post-sale warranty/AMC, events and community, a "
        "LinkedIn content calendar, news signals with lead scoring, and DPDP privacy requests.\n"
        "- A weekly report that builds itself from the activity log.")

st.markdown("#### How it's built")
st.markdown(
    "| Layer | Choice | Why |\n|---|---|---|\n"
    "| UI | Streamlit (Python), custom theme and components, WCAG 2.1 AA checked | Fast to build, one language |\n"
    "| Data | SQLAlchemy 2 models, Alembic migrations, Postgres in production, SQLite for this demo | The data "
    "model is the backbone and survives a future React front end |\n"
    "| Configuration as data | Tracks, cadences, stages, activity types, products, price list, signal weights "
    "are tables, not code | The team changes their process without a developer |\n"
    "| Documents | python-docx (quotes, weekly report), openpyxl (exports), matplotlib (charts, GTM map) | "
    "Leadership gets Word and Excel, not screenshots |\n"
    "| Tests | 150+ pytest checks on a throwaway database | Every slice shipped with its checks |\n"
    "| This demo | The same code with a fictional brand; a simulation replays twelve weeks of work through the app's "
    "own services every day | Realistic, always-current data with no real person in it |")
if repo_url:
    st.link_button("View the source code", repo_url, icon=":material/code:")
