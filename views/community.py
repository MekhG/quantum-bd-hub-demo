"""Community: everyone who has taken part in our events — searchable by interests, level, organisation and events,
with mailing lists that respect consent, profiles, and a one-click lead for a member's organisation."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import CommunityMember, Event, EventParticipant, Lead, Track, User
from app.services.community import (
    ROLE_LABELS,
    STATUS_LABELS,
    CommunityError,
    create_lead_for_member,
    delete_member,
    mailing_list,
    members_frame,
    set_do_not_contact,
    update_member,
)
from app.services.lead_queries import frame_to_xlsx
from app.ui.common import require_user
from app.ui.components import avatar, card, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
FLASH = "edit_flash"


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=3600, show_spinner="Loading the community…")
def _members() -> pd.DataFrame:
    with session_scope() as session:
        return members_frame(session)


@st.cache_data(ttl=3600, show_spinner=False)
def _tracks() -> list[tuple[int, str]]:
    with session_scope() as session:
        return [(t.id, t.name) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                         .order_by(Track.sort_order))]


def _member_detail(member_id: int) -> dict | None:
    with session_scope() as session:
        m = session.get(CommunityMember, member_id)
        if m is None:
            return None
        events = session.execute(
            select(Event.name, Event.kind, Event.start_on, EventParticipant.team_name, EventParticipant.role,
                   EventParticipant.status, EventParticipant.project_title, EventParticipant.project_link)
            .join(Event, Event.id == EventParticipant.event_id).where(EventParticipant.member_id == member_id)
            .order_by(Event.start_on.desc())).all()
        leads = session.execute(select(Lead.id, Track.name).join(Track, Track.id == Lead.track_id)
                                .where(Lead.org_id == m.org_id)).all() if m.org_id else []
        return {
            "id": m.id, "name": m.name, "email": m.email, "phone": m.phone, "education": m.education,
            "level": m.level, "institution": m.institution, "org": m.organisation.name if m.organisation else None,
            "city": m.city, "state": m.state, "interests": m.interests, "github": m.github_url,
            "linkedin": m.linkedin_url, "website": m.website_url, "projects": (m.project_links or "").splitlines(),
            "consent": m.consent, "consent_on": m.consent_on, "adult": m.is_adult, "dnc": m.do_not_contact,
            "source": m.source, "notes": m.notes, "leads": leads,
            "events": [{"Event": n, "Type": k, "Date": d, "Team": t or "", "Role": ROLE_LABELS.get(r, r),
                        "Result": STATUS_LABELS.get(s, s), "Project": p or "", "Link": link or ""}
                       for n, k, d, t, r, s, p, link in events],
        }


def profile(member_id: int) -> None:
    m = _member_detail(member_id)
    if m is None:
        st.warning("That person is no longer in the community.")
        return
    can = user.can("member")
    with card(f"member-{member_id}"):
        top, actions = st.columns([3, 2], vertical_alignment="top")
        mailable = m["consent"] and m["adult"] and not m["dnc"]
        state = ("Mailable", "green") if mailable else (
            ("Do not contact", "red") if m["dnc"] else ("Not mailable: " + ("no consent" if not m["consent"]
                                                                            else "18+ not confirmed"), "orange"))
        links = [f'<a href="{esc(u)}" target="_blank" rel="noopener" style="color:inherit">{label}</a>'
                 for label, u in (("GitHub", m["github"]), ("LinkedIn", m["linkedin"]), ("Website", m["website"])) if u]
        with top:
            html_block(f'<div class="qa-row" style="gap:12px;align-items:center">{avatar(m["name"], 46)}'
                       f'<div class="qa-row-main"><b style="font-size:1.15rem">{esc(m["name"])}</b>'
                       f'<span class="qa-small qa-muted">{esc(" · ".join(x for x in (m["education"], m["institution"]) if x))}</span>'
                       f'</div></div>'
                       f'<div style="margin-top:8px">{chips([(m["level"] or "", "purple"), state, m["state"] or ""])}</div>'
                       f'<div class="qa-small" style="margin-top:8px">'
                       f'<a href="mailto:{esc(m["email"])}" style="color:inherit">{esc(m["email"])}</a>'
                       + (f' · {esc(m["phone"])}' if m["phone"] else "") + (" · " + " · ".join(links) if links else "")
                       + "</div>"
                       + (f'<div class="qa-small" style="margin-top:6px"><b>Interests:</b> {esc(m["interests"])}</div>'
                          if m["interests"] else ""))
        with actions, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            with st.popover("Edit", icon=":material/edit:", disabled=not can):
                with st.form(f"edit_member_{member_id}", border=False):
                    name = st.text_input("Name", value=m["name"])
                    education = st.text_input("Education / profession", value=m["education"] or "")
                    institution = st.text_input("Organisation", value=m["institution"] or "")
                    a, b = st.columns(2)
                    city = a.text_input("City", value=m["city"] or "")
                    state_in = b.text_input("State", value=m["state"] or "")
                    interests = st.text_input("Interests", value=m["interests"] or "")
                    github = st.text_input("GitHub", value=m["github"] or "")
                    linkedin = st.text_input("LinkedIn", value=m["linkedin"] or "")
                    website = st.text_input("Website", value=m["website"] or "")
                    consent = st.checkbox("Agreed to be contacted by Demo", value=m["consent"])
                    adult = st.checkbox("Confirmed 18 or older", value=m["adult"])
                    notes = st.text_area("Notes", value=m["notes"] or "", height=70)
                    if st.form_submit_button("Save", type="primary"):
                        with session_scope() as session:
                            update_member(session, member_id, {
                                "name": name, "education": education, "institution": institution, "city": city,
                                "state": state_in, "interests": interests, "github_url": github,
                                "linkedin_url": linkedin, "website_url": website, "consent": consent,
                                "is_adult": adult, "notes": notes}, session.get(User, user.id))
                        _done("Profile saved.")
            with st.popover("Create lead", icon=":material/target:", disabled=not can):
                tracks = _tracks()
                ids = [tid for tid, _ in tracks]
                st.caption(f"Makes **{m['org'] or m['institution'] or 'their organisation'}** a lead. Event "
                           "participation adds 10 points to its score.")
                track = st.selectbox("Track", ids, format_func=dict(tracks).get, key=f"lead_track_{member_id}")
                if st.button("Create lead", type="primary", key=f"lead_go_{member_id}"):
                    try:
                        with session_scope() as session:
                            lead, note = create_lead_for_member(session, member_id, int(track),
                                                                session.get(User, user.id))
                            lead_id = lead.id
                    except (CommunityError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        st.session_state[FLASH] = ("success", note, [])
                        st.cache_data.clear()
                        st.switch_page("views/leads.py", query_params={"lead": str(lead_id)})
            with st.popover("More", icon=":material/more_horiz:", disabled=not can):
                if st.button("Allow contact again" if m["dnc"] else "Do not contact", key=f"dnc_{member_id}",
                             width="stretch"):
                    with session_scope() as session:
                        set_do_not_contact(session, member_id, not m["dnc"], session.get(User, user.id))
                    _done("Updated.")
                if user.can("admin"):
                    st.caption("Deletion request (DPDP): removes the person everywhere and blocks re-import.")
                    if st.button("Delete this person", key=f"del_{member_id}", width="stretch"):
                        with session_scope() as session:
                            name = delete_member(session, member_id, session.get(User, user.id))
                        st.session_state.pop("member_pick", None)
                        _done(f"{name} deleted.")
        if m["leads"]:
            html_block('<div class="qa-small qa-muted" style="margin-top:6px">Their organisation is a lead:</div>')
            with st.container(horizontal=True, gap="small"):
                for lead_id, track in m["leads"]:
                    st.page_link("views/leads.py", label=f"{m['org']} · {track}", icon=":material/target:",
                                 query_params={"lead": int(lead_id)})
        elif m["org"]:
            st.caption(f"In the hub as **{m['org']}** (not a lead yet).")
        if m["events"]:
            st.markdown("**Events**")
            st.dataframe(pd.DataFrame(m["events"]), hide_index=True, width="stretch",
                         column_config={"Date": st.column_config.DateColumn(format="DD MMM YYYY"),
                                        "Link": st.column_config.LinkColumn(display_text="Open")})
        if m["projects"]:
            st.markdown("**Projects**")
            st.markdown("\n".join(f"- [{p}]({p})" for p in m["projects"]))
        st.caption(" · ".join(x for x in (f"Source: {m['source']}" if m["source"] else "",
                                         f"Consent recorded {m['consent_on']:%d %b %Y}" if m["consent_on"] else "")
                              if x))


# ---------------------------------------------------------------- page

page_header("Community", "Everyone who has taken part in our events. Only people who agreed to be contacted and "
            "confirmed they're 18 or older appear in mailing lists.", eyebrow="Marketing")
members = _members()
if members.empty:
    empty_state("No one in the community yet. Import a Google Form's responses on an event.", page="views/events.py",
                label="Go to Events", icon=":material/event:")
    st.stop()

m = st.columns(4)
m[0].metric("Members", f"{len(members):,}", border=True)
m[1].metric("Mailable", f"{int(members['Mailable'].sum()):,}", border=True,
            help="Consent + 18 or older + not on the do-not-contact list.")
m[2].metric("Organisations", f"{members.loc[members['Organisation'] != '', 'Organisation'].nunique():,}", border=True)
m[3].metric("From lead organisations", f"{int(members['Organisation is a lead'].sum()):,}", border=True)

with st.container(border=True):
    r1 = st.columns([2, 1.2, 1.2, 1.2])
    search = r1[0].text_input("Search", placeholder="Name, email, organisation, interest…", icon=":material/search:")
    levels = r1[1].multiselect("Level", sorted(x for x in members["Level"].unique() if x), placeholder="Any level")
    event_names = sorted({n.strip() for names in members["Event names"] for n in names.split(",") if n.strip()})
    events = r1[2].multiselect("Event", event_names, placeholder="Any event")
    states = r1[3].multiselect("State", sorted(x for x in members["State"].unique() if x), placeholder="Any state")
    t1, t2, t3, t4 = st.columns(4)
    mailable_only = t1.toggle("Mailable only")
    github_only = t2.toggle("Has GitHub")
    lead_only = t3.toggle("From lead organisations")
    winners = t4.toggle("Winners")

view = members
if search:
    text = search.lower()
    view = view[view[["Name", "Email", "Organisation", "Interests", "Education"]].apply(
        lambda r: text in " ".join(r).lower(), axis=1)]
if levels:
    view = view[view["Level"].isin(levels)]
if events:
    view = view[view["Event names"].apply(lambda s: any(e in s for e in events))]
if states:
    view = view[view["State"].isin(states)]
if mailable_only:
    view = view[view["Mailable"]]
if github_only:
    view = view[view["GitHub"] != ""]
if lead_only:
    view = view[view["Organisation is a lead"]]
if winners:
    view = view[view["Won"] > 0]

columns = ["Name", "Level", "Organisation", "State", "Interests", "Events", "Won", "GitHub", "LinkedIn", "Mailable"]
picked_event = st.dataframe(
    view[columns], hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    key="community_table", height=min(460, 35 * (len(view) + 1) + 3),
    column_config={"Name": st.column_config.TextColumn(pinned=True, width="medium"),
                   "Interests": st.column_config.TextColumn(width="medium"),
                   "Events": st.column_config.NumberColumn(format="%d", width="small"),
                   "Won": st.column_config.NumberColumn(format="%d", width="small"),
                   "GitHub": st.column_config.LinkColumn(display_text="Open", width="small"),
                   "LinkedIn": st.column_config.LinkColumn(display_text="Open", width="small"),
                   "Mailable": st.column_config.CheckboxColumn(width="small")})
rows = picked_event.selection.rows if picked_event and picked_event.selection else []

mail = mailing_list(view)
stamp = date.today().isoformat()
with st.container(horizontal=True, gap="small", vertical_alignment="center"):
    st.download_button(f"Mailing list · {len(mail)}", data=frame_to_xlsx(mail, "Mailing list"),
                       file_name=f"community-mailing-list-{stamp}.xlsx", icon=":material/mail:",
                       disabled=mail.empty, help="Only the people shown who consented and are 18 or older.")
    with st.popover("Copy emails", icon=":material/content_copy:", disabled=mail.empty):
        st.caption("Mailable people from the list shown, ready to paste into Bcc.")
        st.code(", ".join(mail["Email"]), language=None, wrap_lines=True)
    if user.can("admin"):
        st.download_button("Full directory (admin)", icon=":material/download:",
                           data=frame_to_xlsx(view.drop(columns=["id", "org_id"]), "Community"),
                           file_name=f"community-{stamp}.xlsx",
                           help="Everyone shown, with a Mailable column — for records, not for mailing.")
    st.caption("Tick a person at the left edge to open their profile.")

if rows:
    profile(int(view.iloc[rows[0]]["id"]))
