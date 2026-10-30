"""Every organisation in the hub — universities and colleges, companies, schools, labs, government bodies —
filtered by industry and type. Pick a batch and export it for contact research."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.services.editing import INSTITUTIONS, dropdown_values
from app.services.lead_queries import organisations_frame, research_sheet
from app.ui.common import require_user
from app.ui.edit_table import edit_table
from app.ui.layout import (
    as_list_column,
    blank_missing_text,
    empty_state,
    filter_industry,
    industry_filter,
    page_header,
)

user = require_user()


@st.cache_data(ttl=3600, show_spinner="Loading organisations…")
def load_organisations() -> pd.DataFrame:
    # Cached: a raw list can be tens of thousands of rows. Any save anywhere clears the cache (see app/db.py).
    with session_scope() as session:
        return organisations_frame(session)


@st.cache_data(ttl=3600, show_spinner=False)
def load_dropdowns() -> dict[str, list[str]]:
    with session_scope() as session:
        return dropdown_values(session)


head, tools = st.columns([4, 1.4], vertical_alignment="bottom")
with head:
    page_header(
        "Organisations",
        "Universities, companies, schools, labs and government bodies in one list. Filter by industry and type → "
        "download a research batch → fill in contacts → bring them back on Import as a Lead tracker.",
        eyebrow="Pipeline",
    )
with tools:
    edit_mode = st.toggle("Edit mode", key="orgs_edit", disabled=not user.can("member"),
                          help="Correct names, industries, websites, types, AISHE codes… right in the table.")
orgs = load_organisations()

if orgs.empty:
    empty_state("No organisations yet. Import an organisation list or a lead tracker first.",
                page="views/import_data.py", label="Go to Import", icon=":material/upload_file:")
    st.stop()


def options(frame: pd.DataFrame, column: str) -> list[str]:
    return sorted(x for x in frame[column].dropna().unique() if x)


_EDU = ("o_tiers", "o_managements", "o_areas", "o_categories", "o_lists", "o_university")
_MORE = (*_EDU, "o_no_contacts", "o_website")
more_active = sum(bool(st.session_state.get(k)) for k in _MORE)
track_names = sorted({t for cell in orgs["Lead in tracks"] for t in cell.split(", ") if t})
education = orgs[orgs["Education"]]

with st.container(border=True):
    industries = industry_filter(orgs, key="o_industry")
    r1 = st.columns([2, 1.3, 1, 1.4])
    search = r1[0].text_input("Search name", placeholder="e.g. Technologies, University", icon=":material/search:",
                              value=st.query_params.get("q", ""))  # ?q= comes from the top-bar search
    types = r1[1].multiselect("Type", options(orgs, "Type"), placeholder="All types")
    states = r1[2].multiselect("State", options(orgs, "State"), placeholder="All states")
    lead_filter = r1[3].selectbox(
        "Lead status",
        ["All organisations", "Not a lead yet", *[f"Lead in: {t}" for t in track_names]],
        help="'Lead in: …' shows organisations already in that track, e.g. to research more contacts.",
    )
    with st.expander(f"More filters ({more_active} on)" if more_active else "More filters"):
        with st.container(horizontal=True):
            no_contacts = st.toggle("No contacts yet", key="o_no_contacts")
            with_website = st.toggle("Has a website", key="o_website")
        st.caption(f"**Education details** — for the {len(education):,} universities and colleges. Picking any "
                   "of these shows only higher-education organisations.")
        r2 = st.columns(3)
        tiers = r2[0].multiselect("Tier", options(education, "Tier"), key="o_tiers")
        managements = r2[1].multiselect("Management", options(education, "Management"), key="o_managements",
                                        help="Government institutions usually buy through the PAC route "
                                             "(Demo is not on GeM).")
        areas = r2[2].multiselect("Area", options(education, "Area"), key="o_areas")
        r3 = st.columns(3)
        categories = r3[0].multiselect("Category", options(education, "Category"), key="o_categories",
                                       help="e.g. Affiliated College, Technical/Polytechnic, Teacher Training.")
        list_names = sorted({t for cell in orgs["Lists"].dropna() for t in cell.split(", ") if t})
        lists = r3[1].multiselect("In list", list_names, key="o_lists")
        university = r3[2].text_input("Affiliating university contains", key="o_university")

view = filter_industry(orgs, industries)
if search:
    view = view[view["Name"].str.contains(search, case=False, regex=False)]
if types:
    view = view[view["Type"].isin(types)]
if states:
    view = view[view["State"].isin(states)]
if any(st.session_state.get(k) for k in _EDU):
    view = view[view["Education"]]
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

m = st.columns(4)
m[0].metric("Organisations shown", f"{len(view):,}", border=True, icon=":material/filter_alt:")
m[1].metric("Industries", f"{view['Industry'].replace('', pd.NA).nunique():,}", border=True,
            icon=":material/category:")
m[2].metric("Already leads", f"{int((view['Lead in tracks'] != '').sum()):,}", border=True,
            icon=":material/target:")
m[3].metric("With contacts", f"{int((view['Contacts'] > 0).sum()):,}", border=True, icon=":material/contacts:")

# Education columns only appear when the rows on screen include universities or colleges.
base = ["Name", "Industry", "Type", "State", "Location", "Website", "Lead in tracks", "Contacts"]
edu_columns = ["Tier", "Category", "Management", "Area", "University Name", "AISHE Code", "Lists"]
columns = base + (edu_columns if view["Education"].any() else [])
MAX_EDIT = 1000
if edit_mode:
    choices = load_dropdowns()
    if len(view) > MAX_EDIT:
        st.info(f"{len(view):,} organisations match. Editing works on up to {MAX_EDIT:,} at a time — narrow the "
                f"filters (industry, search, type…). Showing the first {MAX_EDIT:,}.", icon=":material/filter_alt:")
    edit_columns = [c for c in columns if c not in ("Lists", "Lead in tracks", "Contacts")]
    grid = blank_missing_text(view.head(MAX_EDIT).set_index("org_id")[edit_columns + ["_version"]])
    edit_table(
        grid=INSTITUTIONS,
        frame=grid,
        columns=edit_columns,
        label_column="Name",
        user=user,
        key="orgs_grid",
        height=min(560, 35 * (len(grid) + 1) + 3),
        column_config={
            "Name": st.column_config.TextColumn(pinned=True, width="large", required=True),
            "Industry": st.column_config.SelectboxColumn(options=choices.get("industry", [])),
            "Type": st.column_config.SelectboxColumn(options=choices["org_type"], required=True),
            "State": st.column_config.SelectboxColumn(options=choices["state"]),
            "Area": st.column_config.SelectboxColumn(options=choices["area"], width="small"),
            "Tier": st.column_config.SelectboxColumn(options=choices.get("tier", []), width="small"),
            "AISHE Code": st.column_config.TextColumn(help="Format C-12345 / S-12345 / U-0123"),
        },
    )
    st.stop()

display = blank_missing_text(view[columns])
shown = as_list_column(display, "Lead in tracks")
if "Lists" in shown:
    shown = as_list_column(shown, "Lists")
event = st.dataframe(
    shown,
    hide_index=True,
    width="stretch",
    height=min(460, 35 * (len(shown) + 1) + 3),
    on_select="rerun",
    selection_mode="multi-row",
    key="org_table",
    column_config={
        "Name": st.column_config.TextColumn(pinned=True, width="large"),
        "Industry": st.column_config.TextColumn(width="medium"),
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
