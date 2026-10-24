"""The prospect pool from the raw AISHE list: filter, pick a batch, export it for contact research."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.services.editing import INSTITUTIONS, dropdown_values
from app.services.lead_queries import colleges_frame, research_sheet
from app.ui.common import require_user
from app.ui.edit_table import edit_table
from app.ui.layout import as_list_column, blank_missing_text, empty_state, page_header

user = require_user()


@st.cache_data(ttl=300, show_spinner="Loading institutions…")
def load_colleges() -> pd.DataFrame:
    # Cached for 5 minutes: the raw list can be tens of thousands of rows.
    with session_scope() as session:
        return colleges_frame(session)


@st.cache_data(ttl=300, show_spinner=False)
def load_dropdowns() -> dict[str, list[str]]:
    with session_scope() as session:
        return dropdown_values(session)


head, tools = st.columns([4, 1.4], vertical_alignment="bottom")
with head:
    page_header(
        "Colleges",
        "Filter the pool → download a research batch → fill contacts with AI → paste them on Import as a "
        "Lead tracker. The AISHE code links each row back to its college.",
        eyebrow="Pipeline",
    )
with tools:
    edit_mode = st.toggle("Edit mode", key="colleges_edit", disabled=not user.can("member"),
                          help="Correct names, websites, AISHE codes, tier… right in the table.")
    if st.button("Refresh", icon=":material/refresh:", width="stretch"):
        load_colleges.clear()
colleges = load_colleges()

if colleges.empty:
    empty_state("No institutions yet. Import the raw college list first.", page="views/import_data.py",
                label="Go to Import", icon=":material/upload_file:")
    st.stop()


def options(column: str) -> list[str]:
    return sorted(x for x in colleges[column].dropna().unique() if x)


_MORE = ("c_tiers", "c_managements", "c_areas", "c_categories", "c_lists", "c_university", "c_no_contacts",
         "c_website")
more_active = sum(bool(st.session_state.get(k)) for k in _MORE)
track_names = sorted({t for cell in colleges["Lead in tracks"] for t in cell.split(", ") if t})

with st.container(border=True):
    r1 = st.columns([2, 1.3, 1, 1.4])
    search = r1[0].text_input("Search name", placeholder="e.g. Government Degree College", icon=":material/search:",
                                value=st.query_params.get("q", ""))  # ?q= comes from the top-bar search
    states = r1[1].multiselect("State", options("State"), placeholder="All states")
    types = r1[2].multiselect("Type", options("Type"), placeholder="All types")
    lead_filter = r1[3].selectbox(
        "Lead status",
        ["Not a lead yet", "All institutions", *[f"Lead in: {t}" for t in track_names]],
        help="'Lead in: …' shows institutions already in that track, e.g. to research more contacts.",
    )
    with st.expander(f"More filters ({more_active} on)" if more_active else "More filters"):
        r2 = st.columns(3)
        tiers = r2[0].multiselect("Tier", options("Tier"), key="c_tiers")
        managements = r2[1].multiselect("Management", options("Management"), key="c_managements",
                                        help="Government institutions usually buy through the PAC route (Demo is not on GeM).")
        areas = r2[2].multiselect("Area", options("Area"), key="c_areas")
        r3 = st.columns(3)
        categories = r3[0].multiselect("Category", options("Category"), key="c_categories",
                                       help="e.g. Affiliated College, Technical/Polytechnic, Teacher Training.")
        list_names = sorted({t for cell in colleges["Lists"].dropna() for t in cell.split(", ") if t})
        lists = r3[1].multiselect("In list", list_names, key="c_lists")
        university = r3[2].text_input("Affiliating university contains", key="c_university")
        with st.container(horizontal=True):
            no_contacts = st.toggle("No contacts yet", key="c_no_contacts")
            with_website = st.toggle("Has a website", key="c_website")

view = colleges
if search:
    view = view[view["Name"].str.contains(search, case=False, regex=False)]
if states:
    view = view[view["State"].isin(states)]
if types:
    view = view[view["Type"].isin(types)]
if university:
    view = view[view["University Name"].fillna("").str.contains(university, case=False, regex=False)]
if tiers:
    view = view[view["Tier"].isin(tiers)]
if managements:
    view = view[view["Management"].isin(managements)]
if areas:
    view = view[view["Area"].isin(areas)]
if categories:
    view = view[view["Category"].isin(categories)]
if lists:
    view = view[view["Lists"].fillna("").str.split(", ").apply(lambda tags: any(t in tags for t in lists))]
if lead_filter == "Not a lead yet":
    view = view[view["Lead in tracks"] == ""]
elif lead_filter.startswith("Lead in: "):
    wanted = lead_filter.removeprefix("Lead in: ")
    view = view[view["Lead in tracks"].str.split(", ").apply(lambda tracks: wanted in tracks)]
if no_contacts:
    view = view[view["Contacts"] == 0]
if with_website:
    view = view[view["Website"].fillna("") != ""]

m = st.columns(3)
m[0].metric("Institutions shown", f"{len(view):,}", border=True, icon=":material/filter_alt:")
m[1].metric("In the pool", f"{len(colleges):,}", border=True, icon=":material/account_balance:")
m[2].metric("Already leads", f"{int((colleges['Lead in tracks'] != '').sum()):,}", border=True,
            icon=":material/target:")

columns = ["Name", "Type", "Category", "State", "Location", "Area", "Management", "University Name", "Tier",
           "Website", "AISHE Code", "Lists", "Lead in tracks", "Contacts"]
MAX_EDIT = 1000
if edit_mode:
    choices = load_dropdowns()
    if len(view) > MAX_EDIT:
        st.info(f"{len(view):,} institutions match. Editing works on up to {MAX_EDIT:,} at a time — narrow the "
                f"filters (search, state, type…). Showing the first {MAX_EDIT:,}.", icon=":material/filter_alt:")
    grid = blank_missing_text(view.head(MAX_EDIT).set_index("org_id")[columns + ["_version"]])
    edit_table(
        grid=INSTITUTIONS,
        frame=grid,
        columns=columns,
        label_column="Name",
        user=user,
        key="colleges_grid",
        height=min(560, 35 * (len(grid) + 1) + 3),
        column_config={
            "Name": st.column_config.TextColumn(pinned=True, width="large", required=True),
            "Type": st.column_config.SelectboxColumn(options=choices["org_type"], required=True),
            "State": st.column_config.SelectboxColumn(options=choices["state"]),
            "Area": st.column_config.SelectboxColumn(options=choices["area"], width="small"),
            "Tier": st.column_config.SelectboxColumn(options=choices.get("tier", []), width="small"),
            "AISHE Code": st.column_config.TextColumn(help="Format C-12345 / S-12345 / U-0123"),
            "Contacts": st.column_config.NumberColumn(format="%d", width="small"),
        },
    )
    st.stop()

display = blank_missing_text(view[columns])
shown = as_list_column(as_list_column(display, "Lists"), "Lead in tracks")
event = st.dataframe(
    shown,
    hide_index=True,
    width="stretch",
    height=min(460, 35 * (len(shown) + 1) + 3),
    on_select="rerun",
    selection_mode="multi-row",
    key="college_table",
    column_config={
        "Name": st.column_config.TextColumn(pinned=True, width="large"),
        "Website": st.column_config.LinkColumn(display_text=r"https?://(?:www\.)?([^/]+)"),
        "Lists": st.column_config.ListColumn(),
        "Lead in tracks": st.column_config.ListColumn(),
        "Contacts": st.column_config.NumberColumn(format="%d", width="small"),
    },
)

MAX_BATCH = 500
picked = event.selection.rows if event and event.selection else []
batch = view.iloc[picked] if picked else view.head(MAX_BATCH)
label = (f"Download research batch — {len(batch)} selected" if picked
         else f"Download research batch — first {len(batch)} shown")
with st.container(horizontal=True, vertical_alignment="center"):
    st.download_button(
        label,
        data=research_sheet(batch),
        file_name=f"research-batch-{date.today().isoformat()}.xlsx",
        type="primary",
        disabled=batch.empty,
        icon=":material/download:",
    )
    if not picked and len(view) > MAX_BATCH:
        st.caption(f"Tick rows on the left edge or narrow the filters — batches are capped at {MAX_BATCH}.")
