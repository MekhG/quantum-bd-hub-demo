"""Who changed what, and when — every edit made in the tables, newest first."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from app.db import session_scope
from app.services.lead_queries import audit_frame, frame_to_xlsx
from app.ui.common import require_user
from app.ui.layout import empty_state, page_header

require_user()
page_header("History", "Every edit, addition and deletion made in the Leads, Colleges and Contacts tables. "
            "Imports are listed on the Import page.", eyebrow="Data")


@st.cache_data(ttl=30, show_spinner="Loading history…")
def load_history():
    with session_scope() as session:
        return audit_frame(session, limit=3000)


history = load_history()
if history.empty:
    empty_state("Nothing edited yet. Changes made in edit mode appear here.", icon=":material/history:")
    st.stop()

with st.container(border=True):
    r = st.columns([1.2, 1.2, 1.2, 2])
    what = r[0].multiselect("What", sorted(history["What"].unique()), placeholder="Everything")
    who = r[1].multiselect("By", sorted(x for x in history["By"].unique() if x), placeholder="Anyone")
    since = r[2].date_input("Since", value=date.today() - timedelta(days=30), format="DD/MM/YYYY")
    search = r[3].text_input("Search", placeholder="Field or value, e.g. Stage or Kabir", icon=":material/search:")

view = history[history["When (IST)"].dt.date >= since]
if what:
    view = view[view["What"].isin(what)]
if who:
    view = view[view["By"].isin(who)]
if search:
    needle = search.lower()
    view = view[(view["Field"] + " " + view["From"] + " " + view["To"]).str.lower().str.contains(needle, regex=False)]

st.dataframe(
    view, hide_index=True, width="stretch", height=min(600, 35 * (len(view) + 1) + 3),
    column_config={"When (IST)": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
                   "ID": st.column_config.TextColumn(width="small")},
)
st.download_button("Download (Excel)", data=frame_to_xlsx(view, "History"),
                   file_name=f"history-{date.today().isoformat()}.xlsx", icon=":material/download:")
