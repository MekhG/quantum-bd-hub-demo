"""Leads: filter, export, edit, start/stop cadences, and each lead's detail (contacts, timeline, deals, changes)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.models import Lead, User
from app.services.cadence import STATUS_LABELS, start_cadence, stop_cadence
from app.services.editing import LEADS, dropdown_values
from app.services.lead_queries import frame_to_xlsx, leads_frame, stage_list
from app.services.signals import hot_score
from app.ui.common import require_user
from app.ui.edit_table import edit_table
from app.ui.layout import as_list_column, blank_missing_text, empty_state, page_header, stage_pills

user = require_user()
FLASH = "edit_flash"
CADENCE_FILTERS = ["Not started", *STATUS_LABELS.values()]


@st.cache_data(ttl=60, show_spinner="Loading leads…")
def load_leads() -> tuple[pd.DataFrame, pd.DataFrame]:
    with session_scope() as session:
        return leads_frame(session), stage_list(session)


@st.cache_data(ttl=300, show_spinner=False)
def load_dropdowns() -> dict[str, list[str]]:
    with session_scope() as session:
        return dropdown_values(session)


@st.cache_data(ttl=300, show_spinner=False)
def load_hot_score() -> int:
    with session_scope() as session:
        return hot_score(session)


def _flash(kind: str, message: str, details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


def start_for(lead_ids: list[int]) -> None:
    with session_scope() as session:
        result = start_cadence(session, lead_ids, session.get(User, user.id))
    details = [f"{name}: {reason}" for name, reason in result.skipped]
    st.session_state[FLASH] = ("success" if result.started else "warning", result.summary(), details,
                               f"{len(details)} lead(s) skipped — why")
    st.cache_data.clear()
    st.rerun()


# A link like /leads?lead=42 (from Today, search, Queries or Calendar) opens that lead's record page.
_linked = st.query_params.get("lead")
if _linked and str(_linked).isdigit():
    from app.ui.lead_record import render_lead_record

    render_lead_record(int(_linked), user)
    st.stop()

head, tools = st.columns([4, 1.4], vertical_alignment="bottom")
with head:
    page_header("Leads", "One row per institution per track. Tick a row to open it; tick several to start "
                "or stop cadences together.", eyebrow="Pipeline")
with tools:
    edit_mode = st.toggle("Edit mode", key="leads_edit", disabled=not user.can("member"),
                          help="Edit stage, owner, tier, potential, product fit, notes… right in the table.")
    if st.button("Refresh", icon=":material/refresh:", width="stretch"):
        load_leads.clear()
leads, stages = load_leads()
hot = load_hot_score()

if leads.empty:
    empty_state("No leads yet. Import a tracker to get started.", page="views/import_data.py",
                label="Go to Import", icon=":material/upload_file:")
    st.stop()


def options(column: str) -> list[str]:
    return sorted(x for x in leads[column].dropna().unique() if x != "")


# Widget keys let us count active filters before drawing the 'More filters' label.
_MORE = ("f_states", "f_types", "f_tiers", "f_potentials", "f_channels", "f_cadence")
more_active = sum(bool(st.session_state.get(k)) for k in _MORE) + bool(st.session_state.get("f_nqm")) + bool(
    st.session_state.get("f_quiet")) + bool(st.session_state.get("f_hot"))

with st.container(border=True):
    r1 = st.columns([2, 1.3, 1.3, 1])
    search = r1[0].text_input("Search institution", placeholder="e.g. Andhra University",
                              icon=":material/search:")
    tracks = r1[1].multiselect("Track", options("Track"), placeholder="All tracks")
    stage_order = stages["name"].tolist()
    stages_chosen = r1[2].multiselect("Stage", [s for s in stage_order if s in set(leads["Stage"])],
                                      placeholder="All stages")
    owners = r1[3].multiselect("Owner", options("Owner"), placeholder="Anyone")
    with st.expander(f"More filters ({more_active} on)" if more_active else "More filters"):
        r2 = st.columns(4)
        states = r2[0].multiselect("State", options("State"), key="f_states")
        types = r2[1].multiselect("Type", options("Type"), key="f_types")
        tiers = r2[2].multiselect("Tier", options("Tier"), key="f_tiers")
        cadence_filter = r2[3].multiselect("Cadence", CADENCE_FILTERS, key="f_cadence")
        r3 = st.columns(3)
        potentials = r3[0].multiselect("Potential", options("Potential"), key="f_potentials")
        channels = r3[1].multiselect("Channel", options("Channel"), key="f_channels")
        quiet_days = r3[2].number_input("No activity for at least (days)", min_value=0, value=0, step=7,
                                        key="f_quiet", help="0 = don't filter. Leads with no activity are included.")
        t1, t2 = st.columns(2)
        nqm_only = t1.toggle("NQM applicants only", key="f_nqm")
        hot_only = t2.toggle(f"Hot leads only (score {hot} or more)", key="f_hot",
                             help="The score adds up the institution's signals (News & signals) and automatic "
                                  "points: an open quote, a recent reply, a demo, the NQM flag.")

view = leads
if search:
    view = view[view["Institution"].str.contains(search, case=False, regex=False)]
for column, chosen in (("Track", tracks), ("Stage", stages_chosen), ("Owner", owners), ("State", states),
                       ("Type", types), ("Tier", tiers), ("Potential", potentials), ("Channel", channels)):
    if chosen:
        view = view[view[column].isin(chosen)]
if cadence_filter:
    labels = view["cadence_status"].map(lambda s: STATUS_LABELS.get(s, "Not started") if s else "Not started")
    view = view[labels.isin(cadence_filter)]
if nqm_only:
    view = view[view["NQM"]]
if hot_only:
    view = view[view["Score"] >= hot]
if quiet_days:
    cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=int(quiet_days))
    last = pd.to_datetime(view["Last activity"])
    view = view[last.isna() | (last < cutoff)]

won_stages = set(stages.loc[stages["outcome"] == "won", "name"])
m = st.columns(4)
m[0].metric("Leads shown", f"{len(view):,}", border=True, icon=":material/target:")
m[1].metric("In cadence", f"{int((view['cadence_status'] == 'active').sum()):,}", border=True,
            icon=":material/autorenew:")
m[2].metric("Clients", f"{int(view['Stage'].isin(won_stages).sum()):,}", border=True, icon=":material/handshake:")
m[3].metric("NQM applicants", f"{int(view['NQM'].sum()):,}", border=True, icon=":material/flag:")

columns = ["Institution", "Track", "Stage", "Score", "Cadence", "Owner", "Tier", "Potential", "Channel", "Type", "State",
           "City", "Contacts", "Product fit", "Interest areas", "Last activity", "Next planned", "In stage since",
           "Source", "NQM"]

if edit_mode:
    choices = load_dropdowns()
    edit_columns = ["Institution", "Track", "Stage", "Owner", "Tier", "Potential", "Channel", "NQM", "Product fit",
                    "Interest areas", "Rejected reason", "Notes", "State", "Contacts"]
    grid = view.set_index("lead_id")
    grid = blank_missing_text(grid[edit_columns + ["_version"]])
    grid["Product fit"] = grid["Product fit"].map(lambda text: [t for t in str(text).split(", ") if t])
    edit_table(
        grid=LEADS,
        frame=grid,
        columns=edit_columns,
        label_column="Institution",
        user=user,
        key="leads_grid",
        height=min(560, 35 * (len(grid) + 1) + 3),
        column_config={
            "Institution": st.column_config.TextColumn(pinned=True, width="large", required=True),
            "Stage": st.column_config.SelectboxColumn(options=choices["stage"], required=True, width="medium"),
            "Owner": st.column_config.SelectboxColumn(options=choices["user"]),
            "Tier": st.column_config.SelectboxColumn(options=choices.get("tier", []), width="small"),
            "Potential": st.column_config.SelectboxColumn(options=choices.get("potential", []), width="small"),
            "Channel": st.column_config.SelectboxColumn(options=choices.get("lead_channel", []), required=True),
            "NQM": st.column_config.CheckboxColumn(width="small"),
            "Product fit": st.column_config.MultiselectColumn(options=choices["product"], width="medium"),
            "Interest areas": st.column_config.TextColumn(width="medium", help="Separate items with ';'"),
            "Rejected reason": st.column_config.SelectboxColumn(
                options=choices["lost_reason"], help="Required when the stage is Rejected."),
            "Notes": st.column_config.TextColumn(width="large"),
            "Contacts": st.column_config.NumberColumn(format="%d", width="small"),
        },
    )
    st.stop()

open_slot = st.container()  # filled after the table: the 'Open lead record' bar shows above it, in view
display = blank_missing_text(view[columns])
shown, stage_config = stage_pills(display, stages)
shown = as_list_column(as_list_column(shown, "Product fit"), "Interest areas", sep="; ")
event = st.dataframe(
    shown,
    hide_index=True,
    width="stretch",
    height=min(440, 35 * (len(shown) + 1) + 3),  # shrink for short lists instead of showing empty rows
    on_select="rerun",
    selection_mode="multi-row",
    key="lead_table",
    column_config={
        "Institution": st.column_config.TextColumn(pinned=True, width="large"),
        "Stage": stage_config,
        "Score": st.column_config.NumberColumn(format="%d", width="small",
                                               help="Signals + automatic points. Click the header to sort."),
        "Cadence": st.column_config.TextColumn(width="medium"),
        "Product fit": st.column_config.ListColumn(width="medium"),
        "Interest areas": st.column_config.ListColumn(width="medium"),
        "Contacts": st.column_config.NumberColumn(format="%d", width="small"),
        "Last activity": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
        "Next planned": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
        "In stage since": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
        "NQM": st.column_config.CheckboxColumn(width="small"),
    },
)
selected = event.selection.rows if event and event.selection else []
selected_ids = [int(view.iloc[i]["lead_id"]) for i in selected]

with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.download_button(f"Download {len(view):,} lead{'' if len(view) == 1 else 's'} (Excel)", data=frame_to_xlsx(view[columns], "Leads"),
                       file_name=f"leads-{date.today().isoformat()}.xlsx", icon=":material/download:")
    if user.can("member"):
        if selected_ids:
            if st.button(f"Start cadence ({len(selected_ids)})", icon=":material/play_arrow:", type="primary"):
                start_for(selected_ids)
            if st.button(f"Stop cadence ({len(selected_ids)})", icon=":material/stop:"):
                stopped = 0
                with session_scope() as session:
                    actor = session.get(User, user.id)
                    for lid in selected_ids:
                        lead_row = session.get(Lead, lid)
                        if lead_row.cadence_status in ("active", "awaiting_reply", "finished"):
                            stop_cadence(session, lead_row, actor)
                            stopped += 1
                _flash("success", f"Cadence stopped for {stopped} lead(s).")
        else:
            with st.popover(f"Start cadence for all {len(view):,} shown", icon=":material/playlist_play:"):
                st.caption("Leads that are already in a cadence, closed, or have no verified contact are skipped. "
                           "With a daily cap set (Configuration), extra leads start on the following days.")
                if st.button("Start", type="primary", key="start_all"):
                    start_for(view["lead_id"].astype(int).tolist())

if len(selected_ids) == 1:
    name = view.loc[view["lead_id"] == selected_ids[0], "Institution"].iloc[0]
    with open_slot, st.container(border=True, horizontal=True, vertical_alignment="center",
                                 key="qa-card-info-open"):
        st.markdown(f"**{name}**")
        if st.button("Open lead record", icon=":material/open_in_new:", type="primary", key="open_record"):
            st.query_params["lead"] = str(selected_ids[0])
            st.rerun()
elif len(selected_ids) > 1:
    open_slot.caption(f"{len(selected_ids)} leads selected. Tick just one to open its record.")
