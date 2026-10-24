"""'New task' and 'Hand off' dialogs, shared by Today, Leads and Calendar."""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from app.db import session_scope
from app.models import User
from app.services.activity_log import loggable_types
from app.services.cadence import IST
from app.services.editing import institution_choices
from app.services.tasks import TaskError, create_task, hand_off, team_users
from app.ui.common import CurrentUser
from app.ui.layout import nearest_time, time_options

FLASH = "edit_flash"


@st.cache_data(ttl=300, show_spinner=False)
def _choices() -> tuple[list[tuple[int, str]], list[str]]:
    with session_scope() as session:
        return team_users(session), loggable_types(session)


def _done(message: str) -> None:
    st.session_state[FLASH] = ("success", message, [])
    st.cache_data.clear()
    st.rerun()


@st.dialog("New task", width="large")
def new_task_dialog(user: CurrentUser, org_id: int | None = None, org_label: str | None = None,
                    lead_id: int | None = None) -> None:
    team, types = _choices()
    title = st.text_input("What needs doing? *", placeholder="e.g. Send quotation for NetSim Q to the HoD")
    c1, c2, c3 = st.columns(3)
    day = c1.date_input("Due", value=datetime.now(IST).date() + timedelta(days=1), format="DD/MM/YYYY")
    times = time_options()
    at = c2.selectbox("At", times, index=times.index(nearest_time(datetime.now(IST).replace(hour=10, minute=0))))
    ids = [uid for uid, _ in team]
    owner = c3.selectbox("For", ids, format_func=dict(team).get, index=ids.index(user.id) if user.id in ids else 0)
    kind = st.selectbox("Type", types, index=types.index("Follow-up task") if "Follow-up task" in types else 0)
    if org_id is None:
        linked = st.toggle("Link to an institution", value=False)
        if linked:
            search = st.text_input("Institution", placeholder="Type at least 3 letters", icon=":material/search:",
                                   key="nt_search")
            with session_scope() as session:
                matches = institution_choices(session, search)
            org_id = st.selectbox("Pick the institution", [m[0] for m in matches], format_func=dict(matches).get,
                                  index=0 if len(matches) == 1 else None, key=f"nt_org_{search.strip().lower()}",
                                  disabled=not matches, placeholder="Choose from the matches")
    else:
        st.caption(f"For **{org_label}**")
    notes = st.text_area("Notes (optional)", height=80)
    if st.button("Create task", type="primary", icon=":material/add_task:", disabled=not title.strip()):
        try:
            with session_scope() as session:
                create_task(session, title=title, day=day, at=datetime.strptime(at, "%H:%M").time(), owner_id=owner,
                            user=session.get(User, user.id), org_id=org_id, lead_id=lead_id, type_name=kind,
                            notes=notes)
        except (TaskError, PermissionError) as error:
            st.error(str(error))
            return
        who = dict(team).get(owner, "")
        _done(f"Task created for {who} on {day.strftime('%a %d %b')}.")


@st.dialog("Hand off task")
def hand_off_dialog(user: CurrentUser, task: dict) -> None:
    team, _ = _choices()
    st.markdown(f"**{task['Task']}** — {task.get('Institution') or 'no institution'}")
    others = [(uid, name) for uid, name in team if uid != task.get("owner_id")]
    target = st.selectbox("Hand it to", [uid for uid, _ in others], format_func=dict(others).get)
    note = st.text_area("Note for them", placeholder="e.g. Please send the brochure and two demo slots", height=90)
    if st.button("Hand off", type="primary", icon=":material/forward:"):
        try:
            with session_scope() as session:
                message = hand_off(session, int(task["activity_id"]), int(target), note, session.get(User, user.id))
        except (TaskError, PermissionError) as error:
            st.error(str(error))
            return
        _done(message)
