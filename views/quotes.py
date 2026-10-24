"""Quotes: every quotation across leads — what's open, expiring, accepted — with the same actions as the lead page."""

from __future__ import annotations

import streamlit as st

from app.db import session_scope
from app.services.cadence import today_ist
from app.services.lead_queries import frame_to_xlsx
from app.services.money import inr
from app.services.procurement import fy_label, period_start_of_fy, pipeline_value, quotes_frame
from app.ui.common import require_user
from app.ui.deals import quote_actions
from app.ui.layout import empty_state, page_header

user = require_user()


@st.cache_data(ttl=60, show_spinner=False)
def load():
    with session_scope() as session:
        return quotes_frame(session)


page_header("Quotes", "Every quotation, with its status. Create quotes from a lead's Deals tab.", eyebrow="Pipeline")
quotes = load()
if quotes.empty:
    empty_state("No quotes yet. Open a lead → Deals → New deal → New quote.", page="views/leads.py",
                label="Go to Leads", icon=":material/target:")
    st.stop()

today = today_ist()
total, weighted = pipeline_value(quotes)
sent = quotes[quotes["Status"] == "Sent"]
expiring = sent[sent["Days left"].fillna(99) <= 7]
fy_start = period_start_of_fy(today)
won = quotes[(quotes["Status"] == "Accepted") & (quotes["Date"] >= fy_start)]
m = st.columns(4)
m[0].metric("Open quotes (excl. GST)", inr(total, decimals=False), border=True,
            help="Sent quotes still waiting for a decision.")
m[1].metric("Weighted by potential", inr(weighted, decimals=False), border=True,
            help="Each open quote × its lead's Potential (e.g. 50–70% counts as 60%).")
m[2].metric("Expiring within 7 days", len(expiring), border=True)
m[3].metric(f"Accepted in FY {fy_label(today)}", inr(won["Value excl. GST"].sum(), decimals=False), border=True)

with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
    statuses = st.multiselect("Status", ["Draft", "Sent", "Expired", "Accepted", "Superseded", "Cancelled"],
                              default=["Draft", "Sent", "Expired"], key="q_status", width=420)
    search = st.text_input("Institution", placeholder="Search", icon=":material/search:", key="q_search", width=300)
view = quotes[quotes["Status"].isin(statuses)] if statuses else quotes
if search:
    view = view[view["Institution"].str.contains(search, case=False, regex=False)]

event = st.dataframe(
    view[["Quote", "Institution", "Deal", "Status", "Value excl. GST", "Total incl. GST", "Date", "Valid until",
          "Days left", "By"]],
    hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="q_table",
    height=min(440, 35 * (len(view) + 1) + 3),
    column_config={"Quote": st.column_config.TextColumn(pinned=True),
                   "Value excl. GST": st.column_config.NumberColumn(format="₹ %d"),
                   "Total incl. GST": st.column_config.NumberColumn(format="₹ %d"),
                   "Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                   "Valid until": st.column_config.DateColumn(format="DD MMM YYYY")})
st.download_button(f"Download {len(view)} quote{'s' if len(view) != 1 else ''} (Excel)",
                   data=frame_to_xlsx(view.drop(columns=["id", "lead_id"]), "Quotes"),
                   file_name=f"quotes-{today.isoformat()}.xlsx", icon=":material/download:")
rows = event.selection.rows if event and event.selection else []
if rows:
    row = view.iloc[rows[0]]
    with st.container(border=True, key="qa-card-info-quote"):
        quote_actions(int(row["id"]), user, show_institution=True)
        st.page_link("views/leads.py", label="Open the lead", icon=":material/open_in_new:",
                     query_params={"lead": int(row["lead_id"])})
else:
    st.caption("Tick a quote to download it, mark it sent, revise it or record the PO.")
