"""Privacy requests (DPDP Act 2023): log a request, find what the hub holds about the person, act (export, stop
contact, delete everywhere) and close it with a record of what was done — within the due date."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.services.cadence import today_ist
from app.models import DataRequest, User
from app.models.compliance import DATA_REQUEST_STATUSES, DATA_REQUEST_TYPES
from app.services.lead_queries import frame_to_xlsx
from app.services.privacy_requests import (
    CHANNELS,
    STATUS_LABELS,
    TYPE_LABELS,
    PrivacyError,
    delete_everywhere,
    due_days,
    export_person,
    find_records,
    log_request,
    requests_frame,
    update_request,
    withdraw_consent,
)
from app.ui.common import require_user
from app.ui.components import card, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user("member")
FLASH = "edit_flash"


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


def _me(session) -> User:
    return session.get(User, user.id)


@st.cache_data(ttl=30, show_spinner=False)
def _requests() -> pd.DataFrame:
    with session_scope() as session:
        return requests_frame(session)


@st.dialog("Log a privacy request", width="large")
def log_dialog() -> None:
    with session_scope() as session:
        days = due_days(session)
    st.caption(f"Due {days} days after it was received. Log it the day it arrives, whatever the channel.")
    request_type = st.selectbox("What are they asking for?", DATA_REQUEST_TYPES, format_func=TYPE_LABELS.get)
    a, b = st.columns(2)
    name = a.text_input("Their name")
    email = b.text_input("Their email", help="Used to find their records. Phone-only requests: ask for an email.")
    a, b = st.columns(2)
    channel = a.selectbox("Received by", CHANNELS)
    received = b.date_input("Received on", value=today_ist(), max_value=today_ist(), format="DD/MM/YYYY")
    notes = st.text_area("What they said", height=80, placeholder="Paste or summarise the request")
    if st.button("Log request", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                request = log_request(session, request_type=request_type, requester_name=name,
                                      requester_email=email, channel=channel, received_on=received, notes=notes,
                                      user=_me(session))
                new_id = request.id
        except (PrivacyError, PermissionError) as error:
            st.error(str(error))
            return
        st.session_state["privacy_open"] = new_id
        _done("Logged. Find their records below and act on it.")


def request_panel(row: dict) -> None:
    rid = int(row["id"])
    with session_scope() as session:
        request = session.get(DataRequest, rid)
        email = request.requester_email if request else None
        status = request.status if request else "open"
    with card(f"privacy-{rid}"):
        tone = "red" if row["Overdue"] else ("green" if row["Status"] == "Done" else "orange")
        due = f"Due {row['Due']:%d %b %Y}" if pd.notna(row["Due"]) else ""
        left = (f"{int(row['Days left'])} day(s) left" if pd.notna(row["Days left"]) and row["Days left"] >= 0
                else "")
        html_block(f'<h3 style="margin:0;padding:0">{esc(row["Name"] or row["Email"])}</h3>'
                   f'<div style="margin-top:4px">{chips([(row["Type"], "purple"), (row["Status"], tone), due, left, row["Channel"]])}</div>'
                   + (f'<div class="qa-small" style="margin-top:6px">{esc(row["Notes"])}</div>' if row["Notes"]
                      else ""))
    if not email:
        st.info("No email on this request, so the hub can't look up records. Ask them for the email they used, "
                "or search Contacts and Community by name.", icon=":material/info:")
    else:
        with session_scope() as session:
            found = find_records(session, email)
        st.markdown(f"**What the hub holds for {esc(email)}**")
        m = st.columns(4)
        m[0].metric("Contact records", len(found["contacts"]), border=True)
        m[1].metric("Community records", len(found["members"]), border=True)
        m[2].metric("Interactions", found["activities"], border=True, help="Activities they took part in.")
        m[3].metric("Event entries", found["participations"], border=True)
        if found["contacts"]:
            st.dataframe(pd.DataFrame(found["contacts"]).drop(columns="id"), hide_index=True, width="stretch")
        if found["members"]:
            st.dataframe(pd.DataFrame(found["members"]).drop(columns="id"), hide_index=True, width="stretch")
        if found["suppressed"]:
            st.caption(f"On the suppression list: {found['suppressed']}.")
        st.markdown("**Act on it**")
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            key = f"pr_export_data_{rid}"
            if key in st.session_state:
                st.download_button("Download their data (Excel)", data=st.session_state[key],
                                   icon=":material/download:", type="primary", key=f"pr_export_{rid}",
                                   file_name=f"personal-data-{today_ist().isoformat()}.xlsx")
            # Built on request (not on every page load) so the audit log records real exports only.
            elif st.button("Export their data", icon=":material/file_export:", key=f"pr_prepare_{rid}",
                           help="For access requests: everything the hub holds, ready to send to them."):
                with session_scope() as session:
                    st.session_state[key] = export_person(session, email, _me(session))
                st.rerun()
            with st.popover("Stop contacting", icon=":material/block:"):
                st.caption("Marks them do-not-contact on every record, records consent as withdrawn and adds the "
                           "email to the suppression list. Their records stay.")
                if st.button("Stop all contact", type="primary", key=f"pr_stop_{rid}"):
                    with session_scope() as session:
                        message = withdraw_consent(session, email, _me(session))
                        update_request(session, rid, status="in_progress" if status == "open" else status,
                                       action_taken=message, user=_me(session))
                    _done(message)
            if user.can("admin"):
                with st.popover("Delete everywhere", icon=":material/delete_forever:"):
                    st.caption("Removes their contact and community records for good. Activities with the "
                               "institution stay, without them. Only a one-way hash of the email is kept so a later "
                               "import can't add them back. **This can't be undone.**")
                    sure = st.checkbox("I've checked these are the right records", key=f"pr_sure_{rid}")
                    if st.button("Delete", type="primary", disabled=not sure, key=f"pr_delete_{rid}"):
                        with session_scope() as session:
                            message = delete_everywhere(session, email, _me(session))
                            update_request(session, rid, status="in_progress" if status == "open" else status,
                                           action_taken=message, user=_me(session))
                        _done(message)
            else:
                st.caption("Deletion is done by an admin.")
        if row["Type"].startswith("Correction"):
            st.caption("Corrections: fix the details on the Contacts or Community page, then close the request "
                       "below saying what changed.")
    with st.form(f"pr_close_{rid}", border=True):
        st.markdown("**Status and record**")
        a, b = st.columns([1, 3])
        new_status = a.selectbox("Status", DATA_REQUEST_STATUSES, format_func=STATUS_LABELS.get,
                                 index=DATA_REQUEST_STATUSES.index(status) if status in DATA_REQUEST_STATUSES else 0)
        action = b.text_area("What was done", value=row["Action taken"], height=80,
                             placeholder="e.g. Sent their data by email on 24 Oct; confirmed deletion by reply.")
        if st.form_submit_button("Save", type="primary", icon=":material/save:"):
            try:
                with session_scope() as session:
                    update_request(session, rid, status=new_status, action_taken=action, user=_me(session))
            except (PrivacyError, PermissionError) as error:
                st.error(str(error))
            else:
                _done("Request updated.")


# ---------------------------------------------------------------- page

head, tools = st.columns([4, 1.4], vertical_alignment="bottom")
with head:
    page_header("Privacy requests", "Requests under the DPDP Act: access, correction, deletion, withdrawing "
                "consent and grievances — logged, acted on and closed on time.", eyebrow="Data")
with tools:
    if st.button("Log a request", icon=":material/add:", type="primary", width="stretch"):
        log_dialog()

requests = _requests()
if requests.empty:
    empty_state("No privacy requests yet. When someone asks what data you hold, to be deleted or to stop being "
                "contacted, log it here the same day.")
    with st.expander("What counts as a request?"):
        st.markdown("Any message — email, call, WhatsApp, letter — where a person asks to **see** their data, "
                    "**correct** it, **delete** it, **stop being contacted**, or **complains** about how their data "
                    "was used. An unsubscribe reply to a cadence email is a withdrawal request.")
    st.stop()

open_rows = requests[requests["Status"].isin(["Open", "In progress"])]
m = st.columns(4)
m[0].metric("Open", len(open_rows), border=True)
m[1].metric("Overdue", int(requests["Overdue"].sum()), border=True)
m[2].metric("Closed this FY", int(((requests["Status"].isin(["Done", "Rejected"])) & (pd.to_datetime(
    requests["Closed"]) >= pd.Timestamp(date(today_ist().year if today_ist().month >= 4 else today_ist().year - 1,
                                             4, 1)))).sum()), border=True)
m[3].metric("All requests", len(requests), border=True)

show_closed = st.toggle("Show closed requests", key="pr_closed")
view = requests if show_closed else open_rows
shown = view.drop(columns=["id"])
picked = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="privacy_table", height=min(380, 35 * (len(shown) + 1) + 3),
                      column_config={c: st.column_config.DateColumn(format="DD MMM YYYY")
                                     for c in ("Received", "Due", "Closed")}
                      | {"Overdue": st.column_config.CheckboxColumn(width="small"),
                         "Days left": st.column_config.NumberColumn(format="%d")})
rows = picked.selection.rows if picked and picked.selection else []
st.download_button("Request log (Excel)", data=frame_to_xlsx(requests.drop(columns=["id"]), "Privacy requests"),
                   icon=":material/download:", file_name=f"privacy-requests-{today_ist().isoformat()}.xlsx",
                   help="The log to show an auditor: what was asked, when, and what was done.")
target = None
if rows:
    target = view.iloc[rows[0]].to_dict()
elif st.session_state.get("privacy_open"):
    match = requests[requests["id"] == st.session_state["privacy_open"]]
    target = match.iloc[0].to_dict() if not match.empty else None
if target:
    request_panel(target)
elif view.empty:
    st.caption("No open requests.")
else:
    st.caption("Tick a request at the left edge to work on it.")
