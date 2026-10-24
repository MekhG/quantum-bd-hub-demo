"""Configuration: cadence settings, sales setup (price list, bundles, quotations, checklists, content check),
news & signals, the configuration tables with Excel export, and the weekly backup."""

from __future__ import annotations

from datetime import date

import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import User
from app.services.backup import export_everything
from app.services.cadence_settings import (
    LEAD_OWNER,
    assignment_frame,
    save_assignments,
    save_track_settings,
    settings_frame,
)
from app.services.config_queries import all_config_frames, database_version, frames_to_xlsx
from app.ui.common import require_user
from app.ui.layout import page_header, section_header
from app.ui.intel_setup import render_intel_setup
from app.ui.sales_setup import render_sales_setup

user = require_user()
page_header(
    "Configuration",
    "Tracks, cadences, stages, products, prices and dropdown lists are data, not code — they change as the BD "
    "process changes. Everyone can see them; only an admin can edit.",
    eyebrow="Data",
)

# ---------------------------------------------------------------- cadence settings
section_header("Cadence settings", "Auto-start begins a lead's cadence by itself once one of its contacts is verified. "
               "The daily cap limits new starts per track per day (protects the sender mailbox); extra leads start on "
               "the following days. Leave the cap empty for no limit.")
with session_scope() as session:
    track_settings = settings_frame(session)
    assignments = assignment_frame(session)
    team_names = [u.name for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]
is_admin = user.can("admin")
edited_tracks = st.data_editor(
    track_settings, hide_index=True, width="stretch", key="cadence_settings", placeholder="", disabled=not is_admin or ["Track", "Steps"],
    column_config={
        "Steps": st.column_config.TextColumn(width="large"),
        "Auto-start when verified": st.column_config.CheckboxColumn(width="small"),
        "Daily start cap": st.column_config.NumberColumn(min_value=0, max_value=500, step=1, width="small",
                                                         help="Empty or 0 = no limit"),
    },
)
st.markdown("**Who gets which task**")
st.caption("Each planned cadence task goes to the person set here. 'Lead owner' = the lead's owner "
           "(or whoever started the cadence when the lead has no owner).")
edited_assignments = st.data_editor(
    assignments, hide_index=True, width="stretch", key="task_assignments",
    disabled=not is_admin or ["Track", "Task"],
    column_config={"Goes to": st.column_config.SelectboxColumn(options=[LEAD_OWNER, *team_names], required=True)},
)
if is_admin and st.button("Save cadence settings", type="primary", icon=":material/save:"):
    with session_scope() as session:
        actor = session.get(User, user.id)
        n = save_track_settings(session, edited_tracks, actor) + save_assignments(session, edited_assignments, actor)
    st.session_state["edit_flash"] = ("success", f"Cadence settings saved ({n} change(s)).", [])
    st.rerun()
if not is_admin:
    st.caption("Only an admin can change these.")

st.divider()
section_header("Sales setup", "Price list, bundles, the quotation template and settings, procurement checklists and "
               "the content-check word list.")
render_sales_setup(user)

st.divider()
section_header("News & signals", "Where news comes from, what each signal is worth and how fast it fades, and when "
               "a lead counts as hot.")
render_intel_setup(user)

st.divider()
section_header("Configuration tables")
with session_scope() as session:
    frames = all_config_frames(session)
    version = database_version(session)

st.download_button(
    "Download configuration (Excel)",
    data=frames_to_xlsx(frames),
    file_name=f"quantum-bd-hub-config-{date.today().isoformat()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    icon=":material/download:",
)
tabs = st.tabs(list(frames.keys()))
for tab, (name, frame) in zip(tabs, frames.items()):
    with tab:
        st.dataframe(frame, hide_index=True, width="stretch")
st.caption(f"Database: {version}")

if user.can("admin"):
    st.divider()
    section_header("Backup", "The free database plan keeps no backups. Download everything every Friday and save it "
                   "on your drive — outside the project folder, because it contains contact data.")
    if st.button("Prepare full backup (Excel)", icon=":material/backup:"):
        with st.spinner("Exporting every table…"):
            with session_scope() as session:
                data, counts = export_everything(session, user.id)
        st.session_state["backup_file"] = data
        st.session_state["backup_rows"] = sum(counts.values())
    if st.session_state.get("backup_file"):
        st.download_button(
            f"Download backup — {st.session_state['backup_rows']:,} rows",
            data=st.session_state["backup_file"],
            file_name=f"quantum-bd-hub-backup-{date.today().isoformat()}.xlsx",
            type="primary",
            icon=":material/download:",
            on_click="ignore",
        )
