"""Queries: every enquiry in one queue — reply within a day, follow up, move it to the right track or close it."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Product, Track, User
from app.services.cadence import IST
from app.services.editing import institution_choices
from app.services.lead_queries import frame_to_xlsx
from app.services.queries import CHANNELS, NewQuery, QueryError, close_query, create_query, move_lead_track, queries_frame
from app.ui.common import require_user
from app.ui.layout import empty_state, nearest_time, page_header, time_options

user = require_user()
FLASH = "edit_flash"
STATUS_ORDER = ["Reply overdue", "Awaiting reply", "Following up", "In conversation", "Replied", "Ghosted",
                "Client", "Closed", "Not linked to a lead"]


@st.cache_data(ttl=120, show_spinner="Loading queries…")
def load(days: int) -> pd.DataFrame:
    with session_scope() as session:
        return queries_frame(session, days=days)


@st.cache_data(ttl=3600, show_spinner=False)
def lookups() -> tuple[list[tuple[int, str]], list[str], list[tuple[int, str]]]:
    with session_scope() as session:
        team = [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]
        products = list(session.scalars(select(Product.name).where(Product.is_active.is_(True))
                                        .order_by(Product.sort_order)))
        tracks = [(t.id, t.name) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                           .order_by(Track.sort_order))]
        return team, products, tracks


def _flash(kind: str, message: str) -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


@st.dialog("New query", width="large")
def new_query_dialog() -> None:
    team, products, _ = lookups()
    now = datetime.now(IST)
    c1, c2, c3 = st.columns(3)
    channel = c1.selectbox("Came in by", list(CHANNELS), help="Website enquiries arrive at hello@demo.example.")
    day = c2.date_input("Received on", value=now.date(), format="DD/MM/YYYY")
    times = time_options()
    at = c3.selectbox("At", times, index=times.index(nearest_time(now.time())))
    p1, p2 = st.columns(2)
    name = p1.text_input("Their name *")
    email = p2.text_input("Email")
    phone = p1.text_input("Phone / WhatsApp")
    interest = p2.multiselect("Interested in", products, placeholder="Pick products (optional)")

    st.markdown("**Organisation**")
    org_mode = st.radio("Organisation", ["Already in the hub", "New organisation", "Individual"], horizontal=True,
                        label_visibility="collapsed", key="nq_org_mode")
    org_id = new_org = state = None
    if org_mode == "Already in the hub":
        search = st.text_input("Search", placeholder="Type at least 3 letters", icon=":material/search:",
                               key="nq_search")
        with session_scope() as session:
            matches = institution_choices(session, search)
        org_id = st.selectbox("Pick the organisation", [m[0] for m in matches], format_func=dict(matches).get,
                              index=0 if len(matches) == 1 else None, placeholder="Choose from the matches",
                              key=f"nq_org_{search.strip().lower()}", disabled=not matches)
    elif org_mode == "New organisation":
        n1, n2 = st.columns([2, 1])
        new_org = n1.text_input("Organisation name")
        state = n2.text_input("State (optional)")
    else:
        st.caption("Saved as an individual — e.g. a researcher or professional asking about a Mini product.")

    message = st.text_area("Their message", height=110, placeholder="Paste the enquiry text")
    ids = [uid for uid, _ in team]
    owner = st.selectbox("Who replies", ids, format_func=dict(team).get,
                         index=ids.index(user.id) if user.id in ids else 0,
                         help="Reply target: 1 day. Follow-ups: email → call + WhatsApp → call/WhatsApp → email.")
    if st.button("Save query", type="primary", icon=":material/check:", disabled=not (name.strip() or email.strip())):
        try:
            with session_scope() as session:
                _, messages = create_query(session, NewQuery(
                    channel=channel, received_at=datetime.combine(day, datetime.strptime(at, "%H:%M").time(),
                                                                  tzinfo=IST),
                    name=name, email=email or None, phone=phone or None, org_id=org_id, new_org_name=new_org,
                    state=state, products=interest, message=message or None, owner_id=owner,
                ), session.get(User, user.id))
        except (QueryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash("success", "Query saved. " + " ".join(messages))


# ---------------------------------------------------------------- page

head, tools = st.columns([4, 1.6], vertical_alignment="bottom")
with head:
    page_header("Queries", "Enquiries from the website inbox, email, WhatsApp, phone and events. "
                "Reply within 1 day; the follow-ups are planned for you.", eyebrow="Pipeline")
with tools:
    days = st.selectbox("Period", [30, 90, 365], index=1, format_func=lambda d: f"Last {d} days",
                        label_visibility="collapsed")
    if st.button("New query", icon=":material/add_comment:", type="primary", width="stretch",
                 disabled=not user.can("member")):
        new_query_dialog()
if st.query_params.get("new") == "query":
    # Opened from the top bar's "+ New" menu.
    del st.query_params["new"]
    if user.can("member"):
        new_query_dialog()


queries = load(days)
if queries.empty:
    empty_state("No queries yet. Add one with New query, or import website enquiries on Import → Responses.",
                icon=":material/forum:")
    st.stop()

overdue = int((queries["Status"] == "Reply overdue").sum())
waiting = int((queries["Status"] == "Awaiting reply").sum())
replied_hours = queries["First reply after (h)"].dropna()
m = st.columns(4)
m[0].metric("Reply overdue", overdue, border=True, icon=":material/warning:",
            help="Older than 1 day with no reply from us.")
m[1].metric("Awaiting reply", waiting, border=True, icon=":material/mark_email_unread:")
m[2].metric(f"Queries ({days} days)", len(queries), border=True, icon=":material/forum:")
m[3].metric("Typical first reply", f"{replied_hours.median():.0f} h" if not replied_hours.empty else "—",
            border=True, icon=":material/timer:", help="Median hours from receiving a query to our first reply.")

with st.container(border=True):
    f1, f2, f3 = st.columns([2, 1.3, 1.3])
    search = f1.text_input("Search", placeholder="Name, email, organisation or message", icon=":material/search:")
    present = [s for s in STATUS_ORDER if s in set(queries["Status"])] + \
        sorted(s for s in set(queries["Status"]) if s not in STATUS_ORDER)
    statuses = f2.multiselect("Status", present, placeholder="All statuses")
    channels = f3.multiselect("Came in by", sorted(queries["Channel"].unique()), placeholder="All channels")

view = queries
if search:
    needle = search.lower()
    text = (view["From"] + " " + view["Email"] + " " + view["Organisation"] + " " + view["Message"]).str.lower()
    view = view[text.str.contains(needle, regex=False)]
if statuses:
    view = view[view["Status"].isin(statuses)]
if channels:
    view = view[view["Channel"].isin(channels)]
view = view.assign(_rank=view["Status"].map({s: i for i, s in enumerate(STATUS_ORDER)}).fillna(99)) \
    .sort_values(["_rank", "Received"], ascending=[True, False]).drop(columns="_rank")

columns = ["Status", "Received", "Channel", "From", "Organisation", "Email", "Phone", "Message", "Track", "Stage",
           "First reply after (h)", "Owner"]
event = st.dataframe(
    view[columns], hide_index=True, width="stretch", height=min(480, 35 * (len(view) + 1) + 3),
    on_select="rerun", selection_mode="single-row", key="queries_table",
    column_config={
        "Status": st.column_config.TextColumn(width="medium"),
        "Received": st.column_config.DatetimeColumn(format="DD MMM, HH:mm"),
        "Message": st.column_config.TextColumn(width="large"),
        "First reply after (h)": st.column_config.NumberColumn(format="%.1f h", width="small"),
    },
)
st.download_button(f"Download {len(view):,} {'query' if len(view) == 1 else 'queries'} (Excel)", data=frame_to_xlsx(view[columns], "Queries"),
                   file_name=f"queries-{datetime.now(IST).date().isoformat()}.xlsx", icon=":material/download:")

picked = event.selection.rows if event and event.selection else []
if not picked:
    st.caption("Tick a query to open it, close it, or move it to the right track.")
    st.stop()

row = view.iloc[picked[0]]
with st.container(border=True):
    st.subheader(f"{row['From'] or row['Email']} · {row['Organisation']}", anchor=False)
    st.caption(f"{row['Channel']} query · received {row['Received'].strftime('%d %b %Y, %H:%M')} · "
               f"status **{row['Status']}** · owner {row['Owner'] or '—'}")
    reach = [x for x in (f"[{row['Email']}](mailto:{row['Email']})" if row["Email"] else "",
                         f"[{row['Phone']}](tel:{row['Phone']})" if row["Phone"] else "") if x]
    if reach:
        st.markdown(" · ".join(reach))
    if row["Message"]:
        st.text(row["Message"])
    if pd.isna(row["lead_id"]):
        st.caption("This query isn't linked to a lead.")
        st.stop()
    lead_id = int(row["lead_id"])
    a1, a2, a3 = st.columns(3)
    a1.page_link("views/leads.py", label="Open the lead (timeline, reply task)", icon=":material/open_in_new:",
                 query_params={"lead": lead_id})
    _, _, tracks = lookups()
    with a2.popover("Move to track", icon=":material/swap_horiz:", disabled=not user.can("member"),
                    width="stretch"):
        st.caption("Once it's clear what they need, move the lead to its proper track (e.g. Mini products).")
        target = st.selectbox("Track", [tid for tid, _ in tracks], format_func=dict(tracks).get, key="move_track")
        if st.button("Move", type="primary", key="move_go"):
            try:
                with session_scope() as session:
                    note = move_lead_track(session, lead_id, int(target), session.get(User, user.id))
            except (QueryError, PermissionError) as error:
                st.error(str(error))
            else:
                _flash("success", note)
    with a3.popover("Close query", icon=":material/block:", disabled=not user.can("member"), width="stretch"):
        reason = st.radio("Why?", ["Not relevant", "Spam / duplicate", "Other"], key="close_reason")
        if st.button("Close as Rejected", type="primary", key="close_go"):
            with session_scope() as session:
                close_query(session, lead_id, reason, session.get(User, user.id))
            _flash("success", f"Query closed ({reason}).")
