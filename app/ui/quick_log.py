"""The 'Log activity' form: a call, WhatsApp chat, meeting, demo or enquiry in a few taps (works on a phone)."""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import User
from app.services.activity_log import (
    INBOUND_TYPES,
    LogEntry,
    LogError,
    NextStep,
    log_activity,
    loggable_types,
    org_leads_and_contacts,
)
from app.services.cadence import IST
from app.services.editing import institution_choices
from app.ui.common import CurrentUser
from app.ui.layout import nearest_time, time_options

FLASH = "edit_flash"  # shared with the edit tables, shown by show_edit_flash()
_DIRECTIONS = {"We reached out": "outbound", "They reached out": "inbound", "Internal": "internal"}


@st.cache_data(ttl=3600, show_spinner=False)
def _team() -> list[tuple[int, str]]:
    with session_scope() as session:
        return [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]


@st.cache_data(ttl=3600, show_spinner=False)
def _types() -> list[str]:
    with session_scope() as session:
        return loggable_types(session)


_SCRIPT_KIND = {"Email": "email", "Phone call": "call", "WhatsApp message": "whatsapp", "WhatsApp call": "call",
                "LinkedIn message": "linkedin"}


@st.cache_data(ttl=600, show_spinner=False)
def _library_choices() -> tuple[list[tuple[int, str]], dict[str, list[tuple[int, str]]]]:
    from app.services.library import shareable_documents, usable_scripts

    with session_scope() as session:
        docs = shareable_documents(session)
        scripts = {kind: usable_scripts(session, kind) for kind in set(_SCRIPT_KIND.values())}
    return docs, scripts


def objection_helper(key: str) -> None:
    """Search the objection library while on a call; 'Used it' bumps it up the list for everyone."""
    from app.services.library import mark_objection_used, objections_frame

    with st.expander("Objection help", icon=":material/support:"):
        text = st.text_input("They said…", placeholder="e.g. budget, GeM, delivery, faculty", key=f"{key}_obj_q")
        with session_scope() as session:
            found = objections_frame(session, text=text or None)
        for row in found.head(4).to_dict("records"):
            st.markdown(f"**{row['They say']}**")
            st.markdown(row["We say"])
            if st.button("Used it", key=f"{key}_obj_{row['id']}", type="tertiary", icon=":material/thumb_up:"):
                with session_scope() as session:
                    mark_objection_used(session, int(row["id"]))
                st.toast("Noted — thanks.")
        if found.empty:
            st.caption("Nothing matches. Add answers on Library → Objections.")


@st.dialog("Log activity", width="large")
def quick_log_dialog(user: CurrentUser, org_id: int | None = None, org_label: str | None = None,
                     lead_id: int | None = None) -> None:
    if org_id is None:
        search = st.text_input("Organisation", placeholder="Type at least 3 letters, e.g. Swarna",
                               icon=":material/search:", key="ql_search")
        with session_scope() as session:
            matches = institution_choices(session, search)
        org_id = st.selectbox("Pick the organisation", [m[0] for m in matches], format_func=dict(matches).get,
                              index=0 if len(matches) == 1 else None, placeholder="Choose from the matches",
                              disabled=not matches, key=f"ql_org_{search.strip().lower()}")
        if search and len(search.strip()) >= 3 and not matches:
            st.caption("Not found. Add new organisations through Import → Paste rows.")
    else:
        st.markdown(f"**{org_label}**")
    if org_id is None:
        return

    with session_scope() as session:
        leads, contacts = org_leads_and_contacts(session, org_id)
    lead_options = [lid for lid, _ in leads] + [0]
    lead_labels = dict(leads) | {0: "Not linked to a lead"}
    default_lead = lead_options.index(lead_id) if lead_id in lead_options else 0
    left, right = st.columns(2)
    chosen_lead = left.selectbox("Lead (track · stage)", lead_options, format_func=lead_labels.get,
                                 index=default_lead, key="ql_lead")
    types = _types()
    kind = right.selectbox("What happened?", types, index=types.index("Phone call") if "Phone call" in types else 0,
                           key="ql_type")
    people = left.multiselect("With", [cid for cid, _ in contacts], format_func=dict(contacts).get,
                              placeholder="Choose contacts (optional)", key="ql_people")
    default_dir = "They reached out" if kind in INBOUND_TYPES else "We reached out"
    direction = right.radio("Who reached out?", list(_DIRECTIONS), index=list(_DIRECTIONS).index(default_dir),
                            horizontal=True, key=f"ql_dir_{kind}")
    status = left.radio("Status", ["Done", "Plan for later"], horizontal=True, key="ql_status")
    now = datetime.now(IST)
    day = right.date_input("Date", value=now.date(), format="DD/MM/YYYY", key="ql_day")
    times = time_options()
    at_text = left.selectbox("Time", times, index=times.index(nearest_time(now.time())), key="ql_time")
    at = datetime.strptime(at_text, "%H:%M").time()
    duration = right.number_input("Duration (minutes, optional)", min_value=0, value=0, step=5, key="ql_dur")
    outcome = st.text_input("Outcome in a line", placeholder="e.g. HoD interested, wants a demo for the department",
                            key="ql_outcome")
    notes = st.text_area("Notes", height=90, placeholder="What was said, questions asked, next actions…",
                         key="ql_notes")
    docs, scripts = _library_choices()
    d1, d2 = st.columns(2)
    shared = d1.multiselect("Documents shared", [d for d, _ in docs], format_func=dict(docs).get,
                            placeholder="From the Library (approved only)", key="ql_docs",
                            help="Recorded on the lead's Documents tab. Sharing a brochure outside a cadence "
                                 "moves the stage to Sent Brochure.")
    kind_scripts = scripts.get(_SCRIPT_KIND.get(kind, ""), [])
    script = d2.selectbox("Script used", [s for s, _ in kind_scripts], format_func=dict(kind_scripts).get,
                          index=None, placeholder="Optional", key="ql_script", disabled=not kind_scripts)
    if kind in ("Phone call", "WhatsApp call", "Meeting (online)", "Meeting (in person)", "Demo"):
        objection_helper("ql")

    plan_next = st.toggle("Plan a next step", value=False, key="ql_next")
    next_step = None
    if plan_next:
        team = _team()
        n1, n2, n3 = st.columns(3)
        next_kind = n1.selectbox("Next step", types, index=types.index("Follow-up task")
                                 if "Follow-up task" in types else 0, key="ql_next_type")
        next_day = n2.date_input("On", value=now.date() + timedelta(days=2), format="DD/MM/YYYY", key="ql_next_day")
        owner_ids = [uid for uid, _ in team]
        next_owner = n3.selectbox("Who", owner_ids, format_func=dict(team).get,
                                  index=owner_ids.index(user.id) if user.id in owner_ids else 0, key="ql_next_owner")
        next_title = st.text_input("Next step title (optional)", placeholder="e.g. Send quotation for NetSim Q",
                                   key="ql_next_title")
        next_step = NextStep(next_kind, next_day, owner_id=next_owner, title=next_title or None)

    if st.button("Save", type="primary", icon=":material/check:"):
        entry = LogEntry(
            org_id=org_id, lead_id=chosen_lead or None, type_name=kind,
            when=datetime.combine(day, at, tzinfo=IST), direction=_DIRECTIONS[direction],
            status="planned" if status == "Plan for later" else "done", outcome=outcome or None,
            notes=notes or None, contact_ids=list(people), duration_min=duration or None, next_step=next_step,
            document_ids=list(shared), script_id=script,
        )
        try:
            with session_scope() as session:
                _, messages = log_activity(session, entry, session.get(User, user.id))
        except (LogError, PermissionError) as error:
            st.error(str(error))
            return
        st.session_state[FLASH] = ("success", "Saved. " + " ".join(messages), [])
        for key in [k for k in st.session_state if str(k).startswith("ql_")]:
            del st.session_state[key]
        st.cache_data.clear()
        st.rerun()
