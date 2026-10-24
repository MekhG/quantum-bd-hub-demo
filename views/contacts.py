"""Every contact in one place: search, filter, edit, add by hand, export."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.models import Contact, User
from app.services.cadence import mark_bounced
from app.services.editing import (
    CONTACTS,
    EditError,
    NewContact,
    add_contact,
    bulk_set_flag,
    dropdown_values,
    institution_choices,
)
from app.services.importing.runner import COLLECTION_METHODS
from app.services.lead_queries import contacts_frame, frame_to_xlsx
from app.ui.common import require_user
from app.ui.edit_table import edit_table
from app.ui.layout import as_list_column, blank_missing_text, empty_state, page_header

user = require_user()


@st.cache_data(ttl=60, show_spinner="Loading contacts…")
def load_contacts() -> pd.DataFrame:
    with session_scope() as session:
        return contacts_frame(session)


@st.cache_data(ttl=300, show_spinner=False)
def load_dropdowns() -> dict[str, list[str]]:
    with session_scope() as session:
        return dropdown_values(session)


@st.dialog("Add a contact", width="large")
def add_contact_dialog() -> None:
    choices = load_dropdowns()
    search = st.text_input("Institution", placeholder="Type at least 3 letters of the name, e.g. Andhra",
                           icon=":material/search:")
    with session_scope() as session:
        matches = institution_choices(session, search)
    if search and len(search.strip()) >= 3 and not matches:
        st.caption("No institution found. New institutions come in through Import (paste a row with the "
                   "institution and contact).")
    org_id = st.selectbox("Pick the institution", [m[0] for m in matches],
                          format_func=dict(matches).get, index=None, placeholder="Choose from the matches",
                          disabled=not matches)
    left, right = st.columns(2)
    name = left.text_input("Name *")
    email = right.text_input("Email")
    phone = left.text_input("Phone")
    linkedin = right.text_input("LinkedIn URL")
    contact_type = left.selectbox("Contact type", choices.get("contact_type", []), index=None)
    designation = right.text_input("Designation", placeholder="exact wording, e.g. Associate Professor")
    department = left.selectbox("Department", choices.get("department", []), index=None,
                                accept_new_options=True)
    methods = list(COLLECTION_METHODS)
    method = right.selectbox("How did we get these details?", methods, index=methods.index("Other"),
                             help="Saved in the consent log (DPDP Act).")
    notes = st.text_area("Notes", height=80)
    if st.button("Add contact", type="primary", icon=":material/person_add:",
                 disabled=not (org_id and name.strip())):
        try:
            with session_scope() as session:
                contact = add_contact(session, NewContact(
                    org_id=org_id, name=name, email=email, phone=phone, designation=designation,
                    contact_type=contact_type, department=department, linkedin_url=linkedin, notes=notes,
                    collection_method=method,
                ), session.get(User, user.id))
                added = contact.name
        except EditError as error:
            st.error(str(error))
            return
        st.session_state["edit_flash"] = ("success", f"Added {added}.", [])
        load_contacts.clear()
        st.rerun()


head, tools = st.columns([4, 1.4], vertical_alignment="bottom")
with head:
    page_header("Contacts", "Everyone we're talking to, across all institutions and tracks.", eyebrow="Pipeline")
with tools:
    edit_mode = st.toggle("Edit mode", key="contacts_edit", disabled=not user.can("member"),
                          help="Correct emails, phones, designations, mark verified or do-not-contact.")
    if st.button("Add contact", icon=":material/person_add:", width="stretch", disabled=not user.can("member")):
        add_contact_dialog()
if st.query_params.get("new") == "contact":
    # Opened from the top bar's "+ New" menu.
    del st.query_params["new"]
    if user.can("member"):
        add_contact_dialog()

contacts = load_contacts()

if contacts.empty:
    empty_state("No contacts yet. Import a tracker or paste rows on the Import page.", page="views/import_data.py",
                label="Go to Import", icon=":material/upload_file:")
    st.stop()


def options(column: str) -> list[str]:
    return sorted(x for x in contacts[column].dropna().unique() if x)


_MORE = ("k_departments", "k_states", "k_verified", "k_dnc")
more_active = sum(bool(st.session_state.get(k)) for k in _MORE if st.session_state.get(k) not in (None, "Any"))
track_names = sorted({t for cell in contacts["Tracks"] for t in cell.split(", ") if t})

with st.container(border=True):
    r1 = st.columns([2, 1.3, 1.3, 1.2])
    search = r1[0].text_input("Search", placeholder="Name, email, phone or institution", icon=":material/search:",
                                value=st.query_params.get("q", ""))  # ?q= comes from the top-bar search
    tracks = r1[1].multiselect("Track", track_names, placeholder="All tracks")
    types = r1[2].multiselect("Contact type", options("Contact type"), placeholder="All types")
    with_email = r1[3].selectbox("Has", ["Anything", "Email", "Phone", "Email and phone", "No email"])
    with st.expander(f"More filters ({more_active} on)" if more_active else "More filters"):
        r2 = st.columns(4)
        departments = r2[0].multiselect("Department", options("Department"), key="k_departments")
        states = r2[1].multiselect("State", options("State"), key="k_states")
        verified = r2[2].selectbox("Verified", ["Any", "Verified", "Not verified"], key="k_verified")
        include_dnc = r2[3].toggle("Include do-not-contact", key="k_dnc")

view = contacts
if search:
    needle = search.strip().lower()
    haystack = (view["Name"].fillna("") + " " + view["Email"].fillna("") + " " + view["Phone"].fillna("") + " "
                + view["Institution"].fillna("")).str.lower()
    view = view[haystack.str.contains(needle, regex=False)]
if tracks:
    view = view[view["Tracks"].str.split(", ").apply(lambda ts: any(t in ts for t in tracks))]
if types:
    view = view[view["Contact type"].isin(types)]
if departments:
    view = view[view["Department"].isin(departments)]
if states:
    view = view[view["State"].isin(states)]
if verified != "Any":
    view = view[view["Verified"] == (verified == "Verified")]
if not include_dnc:
    view = view[~view["Do not contact"]]
has_email, has_phone = view["Email"].fillna("") != "", view["Phone"].fillna("") != ""
view = {"Email": view[has_email], "Phone": view[has_phone], "Email and phone": view[has_email & has_phone],
        "No email": view[~has_email]}.get(with_email, view)

m = st.columns(4)
m[0].metric("Contacts shown", f"{len(view):,}", border=True, icon=":material/contacts:")
m[1].metric("Institutions", f"{view['Institution'].nunique():,}", border=True, icon=":material/account_balance:")
m[2].metric("Verified", f"{int(view['Verified'].sum()):,}", border=True, icon=":material/verified:")
m[3].metric("With phone", f"{int((view['Phone'].fillna('') != '').sum()):,}", border=True, icon=":material/call:")

columns = ["Name", "Institution", "Contact type", "Designation", "Department", "Email", "Phone", "LinkedIn",
           "Research interests", "Verified", "Email status", "Do not contact", "Notes", "Tracks", "State", "Source"]

if edit_mode:
    choices = load_dropdowns()
    MAX_EDIT = 2000
    if len(view) > MAX_EDIT:
        st.info(f"Showing the first {MAX_EDIT:,} of {len(view):,} for editing — narrow the filters to see others.")
    grid = blank_missing_text(view.head(MAX_EDIT).set_index("contact_id")[columns + ["_version"]])
    edit_table(
        grid=CONTACTS,
        frame=grid,
        columns=columns,
        label_column="Name",
        user=user,
        key="contacts_grid",
        height=min(560, 35 * (len(grid) + 1) + 3),
        column_config={
            "Name": st.column_config.TextColumn(pinned=True, width="medium", required=True),
            "Institution": st.column_config.TextColumn(width="medium"),
            "Contact type": st.column_config.SelectboxColumn(options=choices.get("contact_type", [])),
            "Email": st.column_config.TextColumn(width="medium"),
            "Verified": st.column_config.CheckboxColumn(width="small"),
            "Do not contact": st.column_config.CheckboxColumn(
                width="small", help="Also adds the email to the do-not-contact list used by every import."),
            "Notes": st.column_config.TextColumn(width="large"),
        },
    )
    st.stop()

st.caption("Tick rows at the left edge, then use the buttons below to mark them verified or do-not-contact — "
           "or switch on **Edit mode** to change any cell.")
shown = as_list_column(blank_missing_text(view[columns]), "Tracks")
event = st.dataframe(
    shown,
    hide_index=True,
    width="stretch",
    height=min(520, 35 * (len(shown) + 1) + 3),
    on_select="rerun",
    selection_mode="multi-row",
    key="contacts_table",
    column_config={
        "Name": st.column_config.TextColumn(pinned=True, width="medium"),
        "Institution": st.column_config.TextColumn(width="medium"),
        "Email": st.column_config.TextColumn(width="medium"),
        "LinkedIn": st.column_config.LinkColumn(display_text="Profile"),
        "Verified": st.column_config.CheckboxColumn(width="small"),
        "Do not contact": st.column_config.CheckboxColumn(width="small"),
        "Tracks": st.column_config.ListColumn(),
    },
)
picked = event.selection.rows if event and event.selection else []
chosen = view.iloc[picked].set_index("contact_id") if picked else view.iloc[0:0].set_index("contact_id")


def bulk(column: str, value: bool, done_text: str) -> None:
    with session_scope() as session:
        result = bulk_set_flag(session, CONTACTS, chosen, column, value, "Name", session.get(User, user.id))
    problems = result.conflicts + result.errors
    message = f"{result.saved_rows} contact(s) {done_text}." if result.saved_rows else "Nothing changed."
    st.session_state["edit_flash"] = ("success" if not problems else "warning", message, problems)
    load_contacts.clear()
    st.rerun()


with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    can = user.can("member") and bool(picked)
    n = len(picked)
    if st.button(f"Mark verified ({n})" if n else "Mark verified", icon=":material/verified:", type="primary",
                 disabled=not can, key="bulk_verify"):
        bulk("Verified", True, "marked verified")
    if st.button("Mark not verified", icon=":material/remove_done:", disabled=not can, key="bulk_unverify"):
        bulk("Verified", False, "marked not verified")
    with st.popover("Email bounced", icon=":material/mail_lock:", disabled=not can):
        st.caption(f"{n} contact(s) will be marked bounced and unverified. Cadences with no other verified contact "
                   "stop; others carry on with the next verified person.")
        if st.button("Confirm bounced", type="primary", key="bulk_bounced"):
            notes = []
            with session_scope() as session:
                actor = session.get(User, user.id)
                for contact_id in chosen.index:
                    notes += mark_bounced(session, session.get(Contact, int(contact_id)), actor)
            st.session_state["edit_flash"] = ("success", f"{n} contact(s) marked bounced.", notes,
                                              "What happened to their cadences")
            load_contacts.clear()
            st.rerun()
    with st.popover("Do not contact", icon=":material/block:", disabled=not can):
        st.caption(f"{n} contact(s) will be marked do-not-contact and their emails added to the do-not-contact list, "
                   "so no import or cadence will reach them.")
        if st.button("Confirm", type="primary", key="bulk_dnc"):
            bulk("Do not contact", True, "marked do-not-contact")
        if st.button("Undo: allow contact again", key="bulk_allow"):
            bulk("Do not contact", False, "allowed again")
    st.download_button(
        f"Download {len(view):,} contact{'' if len(view) == 1 else 's'} (Excel)",
        data=frame_to_xlsx(view[columns], "Contacts"),
        file_name=f"contacts-{date.today().isoformat()}.xlsx",
        icon=":material/download:",
    )
