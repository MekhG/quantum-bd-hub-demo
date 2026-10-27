"""Who a visitor can be in the demo, and the welcome page where they choose.

There are no passwords: the demo signs a visitor in as one of Demo's fictional team members so they can see how the
same hub looks to an admin, a member and a read-only viewer.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from app.config import get_setting

SESSION_KEY = "demo_persona"


@dataclass(frozen=True)
class Persona:
    email: str
    name: str
    role: str
    does: str
    try_this: str
    icon: str


PERSONAS: tuple[Persona, ...] = (
    Persona("riya@demo.example", "Riya Sharma", "Admin",
            "Runs BD: calls, demos, quotes and partners. Approves posts, documents and deal registrations; sets "
            "weekly targets.",
            "Open a hot lead, move it to a demo, then build a quotation as a Word file.", ":material/shield_person:"),
    Persona("kabir@demo.example", "Kabir Rao", "Member",
            "Email automation and warm-lead verification; drafts LinkedIn posts.",
            "Download today's email batch, mark it sent, and watch each cadence move on.", ":material/outgoing_mail:"),
    Persona("neha@demo.example", "Neha Iyer", "Member",
            "Lead generation: researches colleges, imports research batches, verifies contacts.",
            "Filter the Colleges pool, download a research batch, and paste rows back on Import.",
            ":material/person_search:"),
    Persona("leadership@demo.example", "Leadership", "Viewer",
            "Read-only: the dashboard, pipeline and the weekly leadership report.",
            "Open Weekly report and download it as a Word file on the letterhead.", ":material/visibility:"),
)


def chosen_email() -> str | None:
    """The persona picked on the welcome page — or named in the link (?as=riya), so a portfolio site can open the
    demo straight into one role, on any page."""
    email = st.session_state.get(SESSION_KEY)
    if email is None:
        wanted = (st.query_params.get("as") or "").strip().lower()
        match = next((p for p in PERSONAS if wanted and (p.email.split("@")[0] == wanted
                                                          or p.name.split()[0].lower() == wanted)), None)
        if match is not None:
            st.session_state[SESSION_KEY] = email = match.email
    return email


def switch_persona() -> None:
    for key in (SESSION_KEY, "current_user", "viewer_landed"):
        st.session_state.pop(key, None)


def _choose(email: str) -> None:
    st.session_state[SESSION_KEY] = email


def welcome_page() -> None:
    """The first thing a visitor sees: what this is, and whose shoes to step into."""
    from app.ui.layout import LOGO

    author = get_setting("DEMO_AUTHOR", "Mekhala Ganguly")
    author_url = get_setting("DEMO_AUTHOR_URL")
    repo_url = get_setting("DEMO_REPO_URL")

    from app.ui.components import html_block, inject_css

    inject_css()  # the same card and banner styles as inside the hub
    _, middle, _ = st.columns([0.6, 3, 0.6])
    with middle:
        st.image(str(LOGO), width=170)
        html_block(
            '<div class="qa-hero"><div class="qa-hero-text"><div class="qa-hero-crumb">Live demo · portfolio project'
            '</div><h1 class="qa-hero-title">BD &amp; Marketing Hub</h1><p class="qa-hero-sub">One app for a small '
            'business-development team selling quantum teaching labs to universities: leads and contacts, follow-up '
            'cadences, every call, email, WhatsApp and meeting on one timeline, a knowledge base, quotations, partners, '
            'events, a content calendar, news signals and a weekly leadership report.</p></div></div>')
        st.caption("**Demo** is a fictional company. Every institution, person, price and number here is made up, and "
                   "the data is rebuilt every day — so click anything.")
        st.markdown("#### Explore as")
        columns = st.columns(2)
        for i, persona in enumerate(PERSONAS):
            with columns[i % 2], st.container(border=True, key=f"qa-card-persona-{i}"):
                st.markdown(f"**{persona.name}** · {persona.role}")
                st.caption(persona.does)
                st.markdown(f"*Try:* {persona.try_this}")
                st.button(f"Explore as {persona.name.split()[0]}", key=f"persona_{i}", icon=persona.icon,
                          type="primary" if i == 0 else "secondary", width="stretch", on_click=_choose,
                          args=(persona.email,))
        with st.expander("A 60-second tour", icon=":material/route:"):
            st.markdown(
                "1. **Today** — your overdue and due tasks. Press **Done** on one: it asks what happened and what's "
                "next, and the cadence plans the following step by itself.\n"
                "2. **Leads** → tick a hot lead → **Open lead record** — the stage bar, every touch on one timeline, "
                "its signals and score.\n"
                "3. **+ New → Log activity** — log a WhatsApp call with a next step; it appears on the timeline and "
                "the calendar.\n"
                "4. **Dashboard** and **Weekly report** — the same activity rolled up for leadership; download the "
                "report as Word or Excel.\n"
                "5. **GTM charts** — the go-to-market map drawn live from the cadence settings.")
        links = [f"Built by **{author}**" + (f" · [portfolio]({author_url})" if author_url else "")]
        if repo_url:
            links.append(f"[source code]({repo_url})")
        links.append("Python · Streamlit · SQLAlchemy · SQLite/Postgres")
        st.caption(" · ".join(links))
    st.stop()
