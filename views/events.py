"""Events: hackathons, workshops, webinars and other events that aren't tied to one lead — each with its
participants, teams and results, and the Google Forms import."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Event, OptionList, User
from app.models.community import EVENT_MODES, EVENT_STATUSES, PARTICIPANT_ROLES
from app.services.community import (
    FIELDS,
    ROLE_LABELS,
    STATUS_LABELS,
    CommunityError,
    EventData,
    add_participant,
    delete_event,
    events_frame,
    guess_mapping,
    mailing_list,
    participants_frame,
    preview_import,
    read_responses,
    run_import,
    save_event,
    save_participants,
)
from app.services.content import idea_from_event
from app.services.lead_queries import frame_to_xlsx
from app.ui.common import require_user
from app.ui.components import card, chip, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
FLASH = "edit_flash"


def _done(message: str, kind: str = "success", details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def _choices() -> tuple[list[str], list[tuple[int, str]]]:
    with session_scope() as session:
        kinds = list(session.scalars(select(OptionList.value).where(OptionList.list_name == "event_kind",
                                                                    OptionList.is_active.is_(True))
                                     .order_by(OptionList.sort_order))) or ["Hackathon", "Workshop", "Other"]
        team = [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]
        return kinds, team


@st.cache_data(ttl=60, show_spinner=False)
def _events() -> pd.DataFrame:
    with session_scope() as session:
        return events_frame(session)


@st.cache_data(ttl=30, show_spinner=False)
def _participants(event_id: int) -> pd.DataFrame:
    with session_scope() as session:
        return participants_frame(session, event_id)


# ---------------------------------------------------------------- the event form


@st.dialog("Event", width="large")
def event_dialog(event_id: int | None = None) -> None:
    kinds, team = _choices()
    current = None
    if event_id:
        with session_scope() as session:
            e = session.get(Event, event_id)
            current = {k: getattr(e, k) for k in ("name", "kind", "start_on", "end_on", "mode", "venue", "partner",
                                                  "form_url", "cost_inr", "status", "owner_user_id", "description",
                                                  "notes")}
    c = current or {}
    name = st.text_input("Event name", value=c.get("name", ""), placeholder="e.g. Quantum Hackathon 2026")
    a, b = st.columns(2)
    kind = a.selectbox("Type", kinds, index=kinds.index(c["kind"]) if c.get("kind") in kinds else 0)
    mode = b.selectbox("Mode", EVENT_MODES, format_func=str.title,
                       index=EVENT_MODES.index(c["mode"]) if c.get("mode") in EVENT_MODES else 0)
    a, b, d = st.columns(3)
    start = a.date_input("Starts", value=c.get("start_on") or date.today() + timedelta(days=14), format="DD/MM/YYYY")
    end = b.date_input("Ends (optional)", value=c.get("end_on"), format="DD/MM/YYYY")
    status = d.selectbox("Status", EVENT_STATUSES, format_func=str.title,
                         index=EVENT_STATUSES.index(c["status"]) if c.get("status") in EVENT_STATUSES else 0,
                         help="Marking an event Done adds an 'Event participation' signal to each participant's "
                              "institution that is a lead.")
    a, b = st.columns(2)
    venue = a.text_input("Venue or platform", value=c.get("venue") or "", placeholder="e.g. Online (Zoom) / Omega "
                                                                                       "University, Kochi")
    partner = b.text_input("Partner (optional)", value=c.get("partner") or "", placeholder="e.g. Brightpath Skills Foundation")
    a, b, d = st.columns([2, 1, 1])
    form_url = a.text_input("Registration form link (optional)", value=c.get("form_url") or "",
                            placeholder="https://forms.gle/…")
    cost = b.text_input("Cost (₹, optional)", value="" if c.get("cost_inr") is None else f"{c['cost_inr']:.0f}")
    ids = [uid for uid, _ in team]
    owner = d.selectbox("Owner", ids, format_func=dict(team).get,
                        index=ids.index(c.get("owner_user_id") or user.id) if (c.get("owner_user_id") or user.id)
                        in ids else 0)
    description = st.text_area("Description (optional)", value=c.get("description") or "", height=80)
    if st.button("Save event", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                event = save_event(session, EventData(name=name, kind=kind, start_on=start, end_on=end, mode=mode,
                                                      venue=venue, partner=partner, form_url=form_url,
                                                      cost_inr=cost, status=status, owner_id=owner,
                                                      description=description), session.get(User, user.id),
                                   event_id=event_id)
                new_id = event.id
        except (CommunityError, PermissionError) as error:
            st.error(str(error))
            return
        st.query_params["event"] = str(new_id)
        _done("Event saved.")


# ---------------------------------------------------------------- import


def import_tab(event_id: int, event_name: str) -> None:
    st.markdown("**Import Google Form responses**")
    st.caption("In Google Forms: **Responses → ⋮ → Download responses (.csv)**, or open the linked Google Sheet and "
               "**File → Download → .xlsx**. People are recognised by email: someone already in the community only "
               "gets blanks filled. Tip: add two questions to your form — *I am 18 or older* and *I agree to be "
               "contacted by Demo* — only people who tick both go on mailing lists.")
    upload = st.file_uploader("Responses file", type=["csv", "xlsx"], key=f"form_upload_{event_id}")
    if upload is None:
        return
    try:
        frame = read_responses(upload.getvalue(), upload.name)
    except CommunityError as error:
        st.error(str(error))
        return
    columns = list(frame.columns)
    guessed = guess_mapping(columns)
    st.markdown(f"**Match the questions** · {len(frame)} response(s)")
    st.caption("The hub guessed from the question wording. Fix any that are wrong; leave blank what the form "
               "didn't ask.")
    mapping: dict[str, str | None] = {}
    keys = list(FIELDS)
    grid = st.columns(3)
    for i, key in enumerate(keys):
        label = FIELDS[key][0] + (" (required)" if key == "email" else "")
        options = ["—"] + columns
        default = guessed.get(key)
        pick = grid[i % 3].selectbox(label, options, index=options.index(default) if default in options else 0,
                                     key=f"map_{event_id}_{key}")
        mapping[key] = None if pick == "—" else pick
    try:
        with session_scope() as session:
            preview = preview_import(session, frame, mapping)
    except CommunityError as error:
        st.error(str(error))
        return
    counts = preview.counts
    html_block(chips([(f"{counts.get('New member', 0)} new", "green"),
                      (f"{counts.get('Existing member', 0)} already in the community", "blue"),
                      (f"{counts.get('Skipped', 0)} skipped", "orange" if counts.get("Skipped") else "neutral")]))
    shown = preview.frame()
    st.dataframe(shown, hide_index=True, width="stretch", height=min(360, 35 * (len(shown) + 1) + 3),
                 column_config={"Row": st.column_config.NumberColumn(format="%d", width="small"),
                                "Mailable": st.column_config.CheckboxColumn(
                                    help="Consent + 18 or older. Others stay on the event but not on mailing lists.")})
    if not mapping.get("consent") or not mapping.get("is_adult"):
        st.warning("The form has no consent or 18+ question matched, so nobody from this file will be mailable. "
                   "They're still recorded as participants.", icon=":material/info:")
    if st.button(f"Import into {event_name}", type="primary", icon=":material/upload:",
                 disabled=not user.can("member") or not (counts.get("New member") or counts.get("Existing member"))):
        try:
            with session_scope() as session:
                _, stats = run_import(session, preview, event_id=event_id, user=session.get(User, user.id),
                                      file_name=upload.name)
        except (CommunityError, PermissionError) as error:
            st.error(str(error))
            return
        _done("Imported: " + ", ".join(f"{n} {k}" for k, n in stats.items() if n) + ". You can undo it on History.")


def add_person_tab(event_id: int) -> None:
    st.markdown("**Add a person by hand**")
    st.caption("Walk-ins, mentors, judges or speakers.")
    with st.form(f"add_person_{event_id}", clear_on_submit=True, border=False):
        a, b = st.columns(2)
        name = a.text_input("Name")
        email = b.text_input("Email")
        a, b, c = st.columns(3)
        institution = a.text_input("Institution")
        education = b.text_input("Education / profession", placeholder="e.g. M.Sc Physics")
        role = c.selectbox("Role", PARTICIPANT_ROLES, format_func=ROLE_LABELS.get)
        team = st.text_input("Team name (optional)")
        consent = st.checkbox("They agreed to be contacted by Demo")
        adult = st.checkbox("They confirmed they are 18 or older")
        if st.form_submit_button("Add", type="primary", disabled=not user.can("member")):
            try:
                with session_scope() as session:
                    add_participant(session, event_id, name=name, email=email, user=session.get(User, user.id),
                                    institution=institution, education=education, team_name=team, role=role,
                                    consent=consent, is_adult=adult)
            except (CommunityError, PermissionError) as error:
                st.error(str(error))
            else:
                _done(f"{name or email} added.")


# ---------------------------------------------------------------- one event


def event_page(event_id: int) -> None:
    events = _events()
    row = events[events["id"] == event_id]
    if row.empty:
        st.warning("That event no longer exists.")
        return
    e = row.iloc[0].to_dict()
    if st.button("All events", icon=":material/arrow_back:", type="tertiary", key="ev_back"):
        del st.query_params["event"]
        st.rerun()
    parts = _participants(event_id)
    with card("event-head"):
        info, actions = st.columns([3, 1.6], vertical_alignment="top")
        when = e["Starts"].strftime("%d %b %Y") + (f" – {e['Ends'].strftime('%d %b %Y')}" if e["Ends"] != e["Starts"]
                                                   else "")
        tone = {"Done": "green", "Cancelled": "red", "Open": "blue"}.get(e["Status"], "neutral")
        tags = [(e["Type"], "purple"), (e["Status"], tone), when, e["Mode"], e["Venue"],
                ("With " + e["Partner"]) if e["Partner"] else ""]
        with info:
            html_block(f'<h1 class="qa-display" style="margin:0;padding:0;font-size:1.8rem">{esc(e["Event"])}</h1>'
                       f'<div style="margin-top:6px">{chips(tags)}</div>')
        with actions, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            if st.button("Edit", icon=":material/edit:", disabled=not user.can("member"), key="ev_edit"):
                event_dialog(event_id)
            if e["Form"]:
                st.link_button("Form", e["Form"], icon=":material/open_in_new:")
            if st.button("Make a post", icon=":material/campaign:", disabled=not user.can("member"), key="ev_post",
                         help="An announcement (before the event) or a recap (after) on the Content page."):
                with session_scope() as session:
                    post_id = idea_from_event(session, event_id, session.get(User, user.id)).id
                st.cache_data.clear()
                st.switch_page("views/content.py", query_params={"post": post_id})
            if user.can("admin"):
                with st.popover("Delete", type="tertiary"):
                    st.caption("Deletes the event and its participant list. The people stay in the community.")
                    if st.button("Delete event", key="ev_delete"):
                        with session_scope() as session:
                            name = delete_event(session, event_id, session.get(User, user.id))
                        del st.query_params["event"]
                        _done(f"Deleted {name}.")
    m = st.columns(5)
    m[0].metric("Participants", len(parts), border=True)
    m[1].metric("Teams", parts.loc[parts["Team"] != "", "Team"].nunique() if not parts.empty else 0, border=True)
    m[2].metric("Attended", int(parts["Status"].isin(["Attended", "Finalist", "Winner"]).sum()) if not parts.empty
                else 0, border=True)
    m[3].metric("Institutions", parts.loc[parts["Institution"] != "", "Institution"].nunique() if not parts.empty
                else 0, border=True)
    m[4].metric("Mailable", int(parts["Mailable"].sum()) if not parts.empty else 0, border=True,
                help="Consent + 18 or older + not on the do-not-contact list.")

    people, teams, importing, adding = st.tabs([f"Participants · {len(parts)}", "Teams", "Import responses",
                                                "Add a person"])
    with people:
        if parts.empty:
            st.caption("No participants yet — import the Google Form responses or add people by hand.")
        else:
            edited = st.data_editor(
                parts, hide_index=True, width="stretch", key=f"parts_{event_id}", placeholder="",
                disabled=not user.can("member") or ["id", "member_id", "Name", "Email", "Institution", "Level",
                                                     "GitHub", "Mailable"],
                column_order=["Name", "Institution", "Level", "Team", "Role", "Status", "Project", "Project link",
                              "GitHub", "Mailable", "Email"],
                height=min(520, 35 * (len(parts) + 1) + 3),
                column_config={
                    "Role": st.column_config.SelectboxColumn(options=list(ROLE_LABELS.values()), required=True),
                    "Status": st.column_config.SelectboxColumn(options=list(STATUS_LABELS.values()), required=True),
                    "Project link": st.column_config.LinkColumn(),
                    "GitHub": st.column_config.LinkColumn(display_text="Open"),
                    "Mailable": st.column_config.CheckboxColumn(width="small"),
                })
            with st.container(horizontal=True, gap="small", vertical_alignment="center"):
                if user.can("member") and st.button("Save changes", type="primary", icon=":material/save:",
                                                    key=f"parts_save_{event_id}"):
                    with session_scope() as session:
                        n = save_participants(session, event_id, edited, session.get(User, user.id))
                    _done(f"Saved {n} change(s).")
                stamp = e["Starts"].isoformat()
                st.download_button("Participants (Excel)", data=frame_to_xlsx(parts.drop(columns=["id", "member_id"]),
                                                                              "Participants"),
                                   file_name=f"participants-{stamp}.xlsx", icon=":material/download:")
                mailable = mailing_list(parts.assign(Interests=""))
                st.download_button(f"Mailing list · {len(mailable)}", data=mailable.to_csv(index=False).encode(),
                                   file_name=f"mailing-list-{stamp}.csv", mime="text/csv", icon=":material/mail:",
                                   disabled=mailable.empty, help="Only people who consented and are 18 or older.")
    with teams:
        named = parts[parts["Team"] != ""] if not parts.empty else parts
        if named.empty:
            st.caption("No team names yet.")
        for team, members in named.groupby("Team"):
            with card(f"team-{event_id}-{team}"):
                result = next((s for s in ("Winner", "Finalist") if s in set(members["Status"])), "")
                project = next((p for p in members["Project"] if p), "")
                link = next((p for p in members["Project link"] if p), "")
                html_block(f'<div style="display:flex;justify-content:space-between;align-items:center">'
                           f'<b>{esc(team)}</b>{chip(result, "green", strong=True) if result else ""}</div>'
                           + (f'<div class="qa-small">{esc(project)}' + (f' · <a href="{esc(link)}" target="_blank" '
                              f'rel="noopener" style="color:inherit">project</a>' if link else "") + "</div>"
                              if project or link else "")
                           + f'<div class="qa-small qa-muted">{esc(", ".join(members["Name"]))}</div>')
    with importing:
        import_tab(event_id, e["Event"])
    with adding:
        add_person_tab(event_id)


# ---------------------------------------------------------------- page

_linked = st.query_params.get("event")
if _linked and str(_linked).isdigit():
    event_page(int(_linked))
    st.stop()

head, tools = st.columns([4, 1.2], vertical_alignment="bottom")
with head:
    page_header("Events", "Hackathons, workshops, webinars and other events — who registered, who came, the teams "
                "and the results. Participants join the community.", eyebrow="Marketing")
with tools:
    if st.button("New event", icon=":material/add:", type="primary", width="stretch", disabled=not user.can("member")):
        event_dialog()

events = _events()
if events.empty:
    empty_state("No events yet. Add one with New event, then import its Google Form responses.")
    st.stop()

upcoming = events[(events["Starts"] >= date.today()) & (events["Status"] != "Cancelled")]
m = st.columns(4)
m[0].metric("Events", len(events), border=True)
m[1].metric("Coming up", len(upcoming), border=True)
m[2].metric("Participants (all events)", int(events["Participants"].sum()), border=True)
m[3].metric("Done this FY", int(((events["Status"] == "Done") & (events["Starts"] >= date(
    date.today().year if date.today().month >= 4 else date.today().year - 1, 4, 1))).sum()), border=True)

kinds = st.multiselect("Type", sorted(events["Type"].unique()), placeholder="All types")
view = events[events["Type"].isin(kinds)] if kinds else events
shown = view.drop(columns=["id", "Form"])
event = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                     key="events_table", height=min(440, 35 * (len(shown) + 1) + 3),
                     column_config={"Starts": st.column_config.DateColumn(format="DD MMM YYYY"),
                                    "Ends": st.column_config.DateColumn(format="DD MMM YYYY"),
                                    "Participants": st.column_config.NumberColumn(format="%d"),
                                    "Teams": st.column_config.NumberColumn(format="%d"),
                                    "Attended": st.column_config.NumberColumn(format="%d")})
picked = event.selection.rows if event and event.selection else []
with st.container(horizontal=True, gap="small", vertical_alignment="center"):
    st.download_button("Events (Excel)", data=frame_to_xlsx(shown, "Events"), icon=":material/download:",
                       file_name=f"events-{date.today().isoformat()}.xlsx")
    if picked:
        chosen = view.iloc[picked[0]]
        if st.button(f"Open {chosen['Event']}", type="primary", icon=":material/open_in_new:", key="ev_open"):
            st.query_params["event"] = str(int(chosen["id"]))
            st.rerun()
    else:
        st.caption("Tick an event at the left edge to open it.")
