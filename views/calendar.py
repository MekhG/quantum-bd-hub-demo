"""Calendar: every planned and done activity on a month / week / day grid, plus a plain agenda list."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from streamlit_calendar import calendar

from app.db import session_scope
from app.models import Activity
from app.services.activity_log import loggable_types
from app.services.cadence import IST, reschedule_activity
from app.services.tasks import CHANNEL_COLOURS, calendar_events, ics_for, team_users
from app.ui.common import require_user
from app.ui.layout import page_header
from app.ui.task_dialog import new_task_dialog

user = require_user()
FLASH = "edit_flash"
WINDOW_BACK, WINDOW_AHEAD = 60, 120  # days of activities loaded around today


@st.cache_data(ttl=300, show_spinner="Loading the calendar…")
def load(owner_id: int | None, types: tuple[str, ...], include_done: bool) -> list[dict]:
    today = datetime.now(IST).date()
    with session_scope() as session:
        return calendar_events(session, today - timedelta(days=WINDOW_BACK), today + timedelta(days=WINDOW_AHEAD),
                               owner_id=owner_id, types=list(types) or None, include_done=include_done)


@st.cache_data(ttl=3600, show_spinner=False)
def lookups() -> tuple[list[tuple[int, str]], list[str]]:
    with session_scope() as session:
        return team_users(session), loggable_types(session)


head, tools = st.columns([3.4, 2.2], vertical_alignment="bottom")
with head:
    page_header("Calendar", "Meetings, demos, calls, emails and tasks — planned and done. Click an event to act on "
                "it.", eyebrow="Overview")
team, types = lookups()
with tools:
    names = ["Mine", "Everyone"] + [name for _, name in team if name != user.name]
    whose = st.selectbox("Whose calendar", names, key="cal_whose")
    if st.button("New task", icon=":material/add_task:", key="cal_task", disabled=not user.can("member")):
        new_task_dialog(user)
owner_id = {"Mine": user.id, "Everyone": None} | {name: uid for uid, name in team}

with st.container(border=True):
    f1, f2 = st.columns([3, 1.2], vertical_alignment="bottom")
    chosen = f1.multiselect("Show only", types, placeholder="All activity types", key="cal_types")
    include_done = f2.toggle("Include done", value=True, key="cal_done")
events = load(owner_id[whose], tuple(chosen), include_done)

legend = {"Email": "email", "Call": "phone", "WhatsApp": "whatsapp", "Meeting": "meeting", "Demo": "demo",
          "Task": "task", "Done": None}
st.markdown(" ".join(
    f"<span style='display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;border-radius:6px;color:#fff;"
    f"background:{CHANNEL_COLOURS.get(ch, '#6B7280') if ch else '#6B7280'};font-size:0.8rem'>{label}</span>"
    for label, ch in legend.items()), unsafe_allow_html=True)

options = {
    "initialView": "timeGridWeek",
    "headerToolbar": {"left": "prev,next today", "center": "title",
                      "right": "dayGridMonth,timeGridWeek,timeGridDay,listWeek"},
    "buttonText": {"today": "Today", "month": "Month", "week": "Week", "day": "Day", "list": "List"},
    "firstDay": 1,
    "slotMinTime": "07:00:00",
    "slotMaxTime": "21:00:00",
    "nowIndicator": True,
    "navLinks": True,
    "height": 680,
    "slotDuration": "00:30:00",
    "eventMinHeight": 26,
    "dayHeaderFormat": {"weekday": "short", "day": "numeric", "month": "short"},
    "titleFormat": {"day": "numeric", "month": "short", "year": "numeric"},
    "eventTimeFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
    "slotLabelFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
}
custom_css = """
    .fc { font-family: Inter, system-ui, sans-serif; font-size: 0.85rem; }
    .fc .fc-button-primary { background:#7A4A26; border-color:#7A4A26; }
    .fc .fc-button-primary:not(:disabled).fc-button-active, .fc .fc-button-primary:hover { background:#5E3819; }
    .fc .fc-toolbar-title { font-size: 1.15rem; font-weight: 700; }
    .fc-event { cursor: pointer; }
"""
state = calendar(events=events, options=options, custom_css=custom_css, key=f"cal_{whose}_{len(events)}")

clicked = (state or {}).get("eventClick", {}).get("event") if isinstance(state, dict) else None
if clicked:
    props = clicked.get("extendedProps", {})
    activity_id = int(props.get("activity_id") or clicked.get("id"))
    with st.container(border=True):
        st.markdown(f"**{props.get('task') or clicked.get('title')}**")
        st.caption(" · ".join(x for x in (props.get("type"), props.get("institution"),
                                          f"For {props['owner']}" if props.get("owner") else "",
                                          "Done" if props.get("status") == "done" else "Planned") if x))
        if props.get("notes"):
            st.text(props["notes"])
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            if props.get("lead_id"):
                st.page_link("views/leads.py", label="Open lead", icon=":material/open_in_new:",
                             query_params={"lead": int(props["lead_id"])})
            with session_scope() as session:
                ics = ics_for(session, [activity_id])
            st.download_button("Add to my calendar (.ics)", data=ics, file_name=f"activity-{activity_id}.ics",
                               mime="text/calendar", icon=":material/event_available:", key=f"ics_{activity_id}")
            if props.get("status") == "planned" and user.can("member"):
                with st.popover("Reschedule", icon=":material/event:"):
                    day = st.date_input("New date", value=datetime.now(IST).date() + timedelta(days=1),
                                        format="DD/MM/YYYY", key=f"cal_day_{activity_id}")
                    if st.button("Move", type="primary", key=f"cal_move_{activity_id}"):
                        with session_scope() as session:
                            reschedule_activity(session, session.get(Activity, activity_id), day)
                        st.session_state[FLASH] = ("success", f"Moved to {day.strftime('%a %d %b')}.", [])
                        st.cache_data.clear()
                        st.rerun()
        if props.get("status") == "planned":
            st.caption("Mark it done on **Today** (or from the lead's timeline).")

# Plain agenda: the same events as a readable list (works with screen readers and on small phones).
st.subheader("Agenda — next 14 days", anchor=False)
today = datetime.now(IST).date()
agenda = pd.DataFrame([
    {"When": pd.Timestamp(e["start"]), "What": e["extendedProps"]["type"], "Task": e["extendedProps"]["task"],
     "Organisation": e["extendedProps"]["institution"], "For": e["extendedProps"]["owner"],
     "Status": e["extendedProps"]["status"].title(), "id": e["extendedProps"]["activity_id"]}
    for e in events
])
if not agenda.empty:
    agenda = agenda[(agenda["When"].dt.date >= today) & (agenda["When"].dt.date <= today + timedelta(days=14))]
if agenda.empty:
    st.caption("Nothing planned in the next 14 days.")
else:
    agenda = agenda.sort_values("When")
    st.dataframe(agenda.drop(columns="id"), hide_index=True, width="stretch",
                 height=min(420, 35 * (len(agenda) + 1) + 3),
                 column_config={"When": st.column_config.DatetimeColumn(format="ddd DD MMM, HH:mm")})
    planned = agenda[agenda["Status"] == "Planned"]
    if not planned.empty:
        with session_scope() as session:
            bundle = ics_for(session, planned["id"].astype(int).tolist())
        st.download_button(f"Add these {len(planned)} to my calendar (.ics)", data=bundle,
                           file_name=f"bd-hub-agenda-{today.isoformat()}.ics", mime="text/calendar",
                           icon=":material/event_available:",
                           help="Import into Google Calendar or Outlook. Re-importing updates the same events.")

with st.expander("How to add the .ics file to Google Calendar"):
    st.markdown("Google Calendar → ⚙ **Settings** → **Import & export** → **Import** → choose the downloaded file → "
                "pick your calendar → **Import**. In Outlook: **File → Open & Export → Import/Export**.")
