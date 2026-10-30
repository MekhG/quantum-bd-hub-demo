"""Library: documents (as links), scripts with fill-in fields, and answers to common objections.

Members add and edit drafts; only an admin approves. The team sees the current approved version first.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import CadenceStep, Contact, Document, Lead, Objection, Organisation, Product, Script, Track, User
from app.services.editing import institution_choices
from app.services.procurement import options
from app.services.library import (
    MERGE_FIELDS,
    can_delete_document,
    delete_document,
    document_share_count,
    DocumentData,
    LibraryError,
    ObjectionData,
    ScriptData,
    add_document,
    add_script,
    approve_document,
    approve_script,
    archive_document,
    archive_script,
    content_flags,
    document_versions,
    documents_frame,
    flags_message,
    merge_values,
    new_document_version,
    new_script_version,
    objections_frame,
    render,
    save_objection,
    script_versions,
    scripts_frame,
    set_objection_status,
    update_document,
    update_script,
)
from app.ui.common import require_user
from app.ui.components import card, chip, chips, esc, html_block
from app.ui.layout import page_header

user = require_user()
FLASH = "edit_flash"
_STATUS_TONE = {"Approved": "green", "Draft": "orange", "Archived": "neutral"}
_KINDS = {"email": "Email", "call": "Call", "whatsapp": "WhatsApp", "linkedin": "LinkedIn"}


def _flash(message: str, kind: str = "success", details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=3600, show_spinner=False)
def _choices() -> dict:
    with session_scope() as session:
        return {
            "products": [(p.id, p.name) for p in session.scalars(select(Product).where(Product.is_active.is_(True))
                                                                 .order_by(Product.sort_order))],
            "tracks": [(t.id, t.name) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                               .order_by(Track.sort_order))],
            "steps": [(s.id, s.track_id, f"{s.step_no}. {s.name}") for s in session.scalars(
                select(CadenceStep).order_by(CadenceStep.track_id, CadenceStep.step_no))],
            "doc_types": options(session, "document_type") or ["Brochure", "Other"],
        }


@st.cache_data(ttl=3600, show_spinner=False)
def _documents(text: str, include_old: bool) -> pd.DataFrame:
    with session_scope() as session:
        return documents_frame(session, text=text or None, include_old=include_old)


@st.cache_data(ttl=3600, show_spinner=False)
def _scripts(text: str, include_old: bool) -> pd.DataFrame:
    with session_scope() as session:
        return scripts_frame(session, text=text or None, include_old=include_old)


@st.cache_data(ttl=3600, show_spinner=False)
def _objections(text: str, product_id: int | None, track_id: int | None) -> pd.DataFrame:
    with session_scope() as session:
        return objections_frame(session, text=text or None, product_id=product_id, track_id=track_id)


def _warn_flags(flags: list[tuple[str, str]]) -> None:
    if flags:
        st.warning("Wording to check before approval: " + flags_message(flags), icon=":material/spellcheck:")


# ---------------------------------------------------------------- documents


def _document_form(prefix: str, current: Document | None = None) -> DocumentData:
    c = _choices()
    title = st.text_input("Title", value=current.title if current else "", key=f"{prefix}_title",
                          placeholder="e.g. QSim Cluster brochure")
    url = st.text_input("Link", value=current.url if current else "", key=f"{prefix}_url",
                        placeholder="https://drive.google.com/… (set sharing to anyone in your team with the link)")
    a, b = st.columns(2)
    types = c["doc_types"]
    doc_type = a.selectbox("Type", types, index=types.index(current.doc_type) if current and current.doc_type in types
                           else 0, key=f"{prefix}_type")
    version_label = b.text_input("Version label (optional)", value=(current.version_label or "") if current else "",
                                 placeholder="e.g. Oct 2026", key=f"{prefix}_ver")
    products = a.multiselect("Products", [p for p, _ in c["products"]], format_func=dict(c["products"]).get,
                             default=[p.id for p in current.products] if current else [], key=f"{prefix}_products")
    tracks = b.multiselect("Tracks / audience", [t for t, _ in c["tracks"]], format_func=dict(c["tracks"]).get,
                           default=[t.id for t in current.tracks] if current else [], key=f"{prefix}_tracks")
    tags = st.text_input("Tags (comma-separated)", value=(current.tags or "") if current else "", key=f"{prefix}_tags",
                         placeholder="e.g. NQM, pricing, schools")
    description = st.text_area("What it is (one or two lines)", value=(current.description or "") if current else "",
                               height=68, key=f"{prefix}_desc")
    key_text = st.text_area("Key text for search (optional)", value=(current.key_text or "") if current else "",
                            height=110, key=f"{prefix}_key",
                            help="Paste the document's main points here so search can find what's inside the link.")
    return DocumentData(title=title, doc_type=doc_type, url=url, description=description, key_text=key_text,
                        tags=tags, version_label=version_label, product_ids=products, track_ids=tracks)


@st.dialog("Add a document", width="large")
def add_document_dialog() -> None:
    data = _document_form("ad")
    if st.button("Save as draft", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                doc, flags = add_document(session, data, session.get(User, user.id))
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash(f"“{data.title}” added as a draft." + (" Check the wording before approval: " + flags_message(flags)
                                                    if flags else " An admin approves it for the team."))


@st.dialog("Edit document", width="large")
def edit_document_dialog(doc_id: int, as_new_version: bool) -> None:
    with session_scope() as session:
        current = session.get(Document, doc_id)
        if current is None:
            st.error("That document no longer exists.")
            return
        _ = (current.products, current.tracks)  # load them while the session is open
        session.expunge(current)
    if as_new_version:
        st.caption("A new version starts as a draft. The approved version stays the one the team sends until an admin "
                   "approves this one.")
    data = _document_form(f"ed{doc_id}{int(as_new_version)}", current)
    if st.button("Save new version" if as_new_version else "Save changes", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                actor = session.get(User, user.id)
                flags = (new_document_version(session, doc_id, data, actor)[1] if as_new_version
                         else update_document(session, doc_id, data, actor))
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash(("New version saved as a draft." if as_new_version else "Saved.")
               + (" Check the wording: " + flags_message(flags) if flags else ""))


def _document_detail(doc_id: int) -> None:
    with session_scope() as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return
        versions = [(d.id, d.version_no, d.status, d.is_current, d.url, d.updated_at,
                     d.approved_by.name if d.approved_by else "") for d in document_versions(session, doc.family_id)]
        info = {"title": doc.title, "type": doc.doc_type, "status": doc.status.title(), "url": doc.url,
                "version": doc.version_no, "label": doc.version_label, "description": doc.description,
                "key_text": doc.key_text, "tags": doc.tags, "products": [p.name for p in doc.products],
                "tracks": [t.name for t in doc.tracks], "owner": doc.owner.name if doc.owner else "",
                "flags": content_flags(session, doc.title, doc.description, doc.key_text),
                "is_draft": doc.status == "draft",
                "can_delete": can_delete_document(doc, session.get(User, user.id)),
                "shared": document_share_count(session, doc.id)}
    with card(f"doc-{doc_id}", "info"):
        tags = [(info["status"], _STATUS_TONE.get(info["status"], "neutral")), info["type"],
                f"v{info['version']}" + (f" · {info['label']}" if info["label"] else "")]
        tags += [(p, "blue") for p in info["products"]] + list(info["tracks"])
        tags += [t.strip() for t in (info["tags"] or "").split(",") if t.strip()]
        html_block(f'<div class="qa-row-title" style="font-size:1.1rem">{esc(info["title"])}</div>'
                   f'<div style="margin:6px 0">{chips(tags)}</div>'
                   + (f'<div>{esc(info["description"])}</div>' if info["description"] else "")
                   + f'<div class="qa-small qa-muted" style="margin-top:4px">Added by {esc(info["owner"])}</div>')
        if info["key_text"]:
            with st.expander("Key text"):
                st.text(info["key_text"])
        _warn_flags(info["flags"])
        can_edit = user.can("member") and (info["is_draft"] or user.can("admin"))
        with st.container(horizontal=True, gap="small"):
            st.link_button("Open", info["url"], icon=":material/open_in_new:", type="primary")
            if st.button("Edit", key=f"doc_edit_{doc_id}", icon=":material/edit:", disabled=not can_edit):
                edit_document_dialog(doc_id, False)
            if st.button("New version", key=f"doc_new_{doc_id}", icon=":material/upgrade:",
                         disabled=not user.can("member")):
                edit_document_dialog(doc_id, True)
            if user.can("admin") and info["is_draft"]:
                with st.popover("Approve", icon=":material/verified:"):
                    keep = st.checkbox("Approve with the flagged wording", key=f"doc_keep_{doc_id}") \
                        if info["flags"] else False
                    if st.button("Approve for the team", type="primary", key=f"doc_ok_{doc_id}"):
                        try:
                            with session_scope() as session:
                                approve_document(session, doc_id, session.get(User, user.id), override_flags=keep)
                        except LibraryError as error:
                            st.error(str(error))
                        else:
                            _flash("Approved — the team now sees this version.")
            if info["can_delete"]:
                with st.popover("Remove", icon=":material/delete:"):
                    if info["shared"]:
                        st.caption(f"Shared with leads {info['shared']} time(s), so it stays for their history"
                                   + (" — use Archive instead." if user.can("admin") else " — ask an admin to "
                                      "archive it."))
                    else:
                        st.caption("Removes this link from the Library for good.")
                        if st.button("Remove link", key=f"doc_del_{doc_id}", type="primary"):
                            try:
                                with session_scope() as session:
                                    message = delete_document(session, doc_id, session.get(User, user.id))
                            except (LibraryError, PermissionError) as error:
                                st.error(str(error))
                            else:
                                _flash(message)
            if user.can("admin") and info["status"] != "Archived":
                with st.popover("Archive", icon=":material/archive:"):
                    st.caption("Hides this version. If it was current, the previous approved version takes over.")
                    if st.button("Archive", key=f"doc_arch_{doc_id}"):
                        with session_scope() as session:
                            archive_document(session, doc_id, session.get(User, user.id))
                        _flash("Archived.")
        if len(versions) > 1:
            with st.expander(f"All versions · {len(versions)}"):
                st.dataframe(pd.DataFrame([{"Version": f"v{v}", "Status": s.title(), "Current": cur, "Link": u,
                                            "Updated": up, "Approved by": ab}
                                           for _, v, s, cur, u, up, ab in versions]),
                             hide_index=True, width="stretch",
                             column_config={"Link": st.column_config.LinkColumn(display_text="Open"),
                                            "Updated": st.column_config.DatetimeColumn(format="DD MMM YYYY")})


def documents_tab() -> None:
    c = _choices()
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        text = st.text_input("Search documents", placeholder="e.g. QKD brochure",
                             icon=":material/search:", key="lib_doc_q", width=420)
        kinds = st.multiselect("Type", c["doc_types"], placeholder="All types", key="lib_doc_types", width=260)
        include_old = st.toggle("Older versions", key="lib_doc_old")
        if st.button("Add document", icon=":material/add_link:", type="primary", disabled=not user.can("member")):
            add_document_dialog()
    docs = _documents(text, include_old)
    if kinds:
        docs = docs[docs["Type"].isin(kinds)]
    if docs.empty:
        with card("no-docs"):
            st.markdown("**No documents yet.**" if not text else f"**Nothing matches “{text}”.**")
            st.caption("Add Google Drive or website links to brochures, spec sheets, decks, proposals and the "
                       "overview video. Set each Drive file's sharing to “anyone in the team with the link”.")
        return
    drafts = int((docs["Status"] == "Draft").sum())
    if drafts and user.can("admin"):
        html_block(chip(f"{drafts} draft{'s' if drafts != 1 else ''} waiting for your approval", "orange", dot=True))
    event = st.dataframe(
        docs[["Title", "Type", "Status", "Version", "Products", "Tracks", "Link", "Shared", "Updated"]],
        hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="lib_doc_table",
        height=min(420, 35 * (len(docs) + 1) + 3),
        column_config={"Title": st.column_config.TextColumn(width="large", pinned=True),
                       "Link": st.column_config.LinkColumn(display_text="Open"),
                       "Shared": st.column_config.NumberColumn(help="Times recorded as shared with a lead"),
                       "Updated": st.column_config.DatetimeColumn(format="DD MMM YYYY")})
    rows = event.selection.rows if event and event.selection else []
    if rows:
        _document_detail(int(docs.iloc[rows[0]]["id"]))
    else:
        st.caption("Tick a document to open it, edit it, add a new version or approve it.")


# ---------------------------------------------------------------- scripts


def _script_form(prefix: str, current: Script | None = None) -> ScriptData:
    c = _choices()
    title = st.text_input("Title", value=current.title if current else "", key=f"{prefix}_title",
                          placeholder="e.g. Colleges — intro email")
    a, b, d = st.columns(3)
    kinds = list(_KINDS)
    kind = a.selectbox("Kind", kinds, format_func=_KINDS.get,
                       index=kinds.index(current.kind) if current else 0, key=f"{prefix}_kind")
    track_ids = [0] + [t for t, _ in c["tracks"]]
    track_labels = {0: "Any track"} | dict(c["tracks"])
    track = b.selectbox("Track", track_ids, format_func=track_labels.get,
                        index=track_ids.index(current.track_id) if current and current.track_id in track_ids else 0,
                        key=f"{prefix}_track")
    steps = [(sid, label) for sid, tid, label in c["steps"] if tid == track]
    step_ids = [0] + [sid for sid, _ in steps]
    step = d.selectbox("Cadence step", step_ids, format_func=({0: "Not tied to a step"} | dict(steps)).get,
                       index=step_ids.index(current.cadence_step_id) if current and current.cadence_step_id in step_ids
                       else 0, key=f"{prefix}_step_{track}", disabled=not steps,
                       help="Tie an email script to a cadence step and the email batch fills it in for every row.")
    subject = st.text_input("Subject", value=(current.subject or "") if current else "", key=f"{prefix}_subject",
                            disabled=kind != "email", placeholder="e.g. Quantum lab for {organisation}")
    body = st.text_area("Text", value=current.body if current else "", height=220, key=f"{prefix}_body",
                        placeholder="Dear {salutation} {last_name},\n\n…\n\nRegards,\n{sender_name}")
    notes = st.text_input("Notes for the team (optional)", value=(current.notes or "") if current else "",
                          key=f"{prefix}_notes")
    with st.expander("Fill-in fields you can use"):
        st.markdown("\n".join(f"- `{{{k}}}` — {v}" for k, v in MERGE_FIELDS.items()))
    return ScriptData(title=title, kind=kind, body=body, subject=subject if kind == "email" else None,
                      track_id=track or None, cadence_step_id=step or None, notes=notes)


@st.dialog("Add a script", width="large")
def add_script_dialog() -> None:
    data = _script_form("as")
    if st.button("Save as draft", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                _, flags = add_script(session, data, session.get(User, user.id))
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash(f"“{data.title}” saved as a draft." + (" Check the wording: " + flags_message(flags) if flags else ""))


@st.dialog("Edit script", width="large")
def edit_script_dialog(script_id: int, as_new_version: bool) -> None:
    with session_scope() as session:
        current = session.get(Script, script_id)
        if current is None:
            st.error("That script no longer exists.")
            return
        session.expunge(current)
    data = _script_form(f"es{script_id}{int(as_new_version)}", current)
    if st.button("Save new version" if as_new_version else "Save changes", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                actor = session.get(User, user.id)
                flags = (new_script_version(session, script_id, data, actor)[1] if as_new_version
                         else update_script(session, script_id, data, actor))
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash(("New version saved as a draft." if as_new_version else "Saved.")
               + (" Check the wording: " + flags_message(flags) if flags else ""))


def _preview(script: dict, key: str) -> None:
    """Fill the script for a real lead's main contact."""
    search = st.text_input("Preview for an organisation", placeholder="Type 3+ letters", key=f"{key}_q",
                           icon=":material/search:")
    with session_scope() as session:
        matches = institution_choices(session, search)
    org_id = st.selectbox("Organisation", [m[0] for m in matches], format_func=dict(matches).get,
                          index=0 if len(matches) == 1 else None, placeholder="Choose", disabled=not matches,
                          key=f"{key}_org_{search.strip().lower()}", label_visibility="collapsed")
    if org_id is None:
        return
    with session_scope() as session:
        org = session.get(Organisation, org_id)
        lead = session.scalar(select(Lead).where(Lead.org_id == org_id).order_by(Lead.id))
        contact = session.get(Contact, lead.primary_contact_id) if lead and lead.primary_contact_id else \
            session.scalar(select(Contact).where(Contact.org_id == org_id).order_by(Contact.id))
        sender = lead.owner.name if lead and lead.owner else None
        mailbox = lead.track.sender_mailbox if lead else None
        values = merge_values(contact=contact, org=org, lead=lead, sender_name=sender, sender_email=mailbox)
    subject, empty1 = render(script["subject"], values)
    body, empty2 = render(script["body"], values)
    if script["subject"]:
        st.markdown(f"**Subject:** {subject}")
    st.code(body, language=None, wrap_lines=True)
    empty = sorted(set(empty1 + empty2))
    if empty:
        st.caption("Empty for this contact: " + ", ".join("{" + e + "}" for e in empty) + " — check the wording reads "
                   "well without them.")


def _script_detail(script_id: int) -> None:
    with session_scope() as session:
        s = session.get(Script, script_id)
        if s is None:
            return
        info = {"title": s.title, "kind": _KINDS.get(s.kind, s.kind), "status": s.status.title(),
                "subject": s.subject, "body": s.body, "notes": s.notes, "version": s.version_no,
                "track": s.track.name if s.track else "Any track", "is_draft": s.status == "draft",
                "flags": content_flags(session, s.subject, s.body),
                "versions": len(script_versions(session, s.family_id))}
    script_tags = [(info["status"], _STATUS_TONE.get(info["status"], "neutral")), info["kind"], info["track"],
                   "v" + str(info["version"])]
    with card(f"script-{script_id}", "info"):
        html_block(f'<div class="qa-row-title" style="font-size:1.1rem">{esc(info["title"])}</div>'
                   f'<div style="margin:6px 0">{chips(script_tags)}</div>')
        if info["subject"]:
            st.markdown(f"**Subject:** {info['subject']}")
        st.code(info["body"], language=None, wrap_lines=True)
        if info["notes"]:
            st.caption(info["notes"])
        _warn_flags(info["flags"])
        with st.container(horizontal=True, gap="small"):
            if st.button("Edit", key=f"sc_edit_{script_id}", icon=":material/edit:",
                         disabled=not (user.can("member") and (info["is_draft"] or user.can("admin")))):
                edit_script_dialog(script_id, False)
            if st.button("New version", key=f"sc_new_{script_id}", icon=":material/upgrade:",
                         disabled=not user.can("member")):
                edit_script_dialog(script_id, True)
            if user.can("admin") and info["is_draft"]:
                with st.popover("Approve", icon=":material/verified:"):
                    keep = st.checkbox("Approve with the flagged wording", key=f"sc_keep_{script_id}") \
                        if info["flags"] else False
                    if st.button("Approve for the team", type="primary", key=f"sc_ok_{script_id}"):
                        try:
                            with session_scope() as session:
                                approve_script(session, script_id, session.get(User, user.id), override_flags=keep)
                        except LibraryError as error:
                            st.error(str(error))
                        else:
                            _flash("Approved — the email batch now uses this version.")
            if user.can("admin") and info["status"] != "Archived":
                with st.popover("Archive", icon=":material/archive:"):
                    if st.button("Archive", key=f"sc_arch_{script_id}"):
                        with session_scope() as session:
                            archive_script(session, script_id, session.get(User, user.id))
                        _flash("Archived.")
        with st.expander("Preview with a real contact", icon=":material/preview:"):
            _preview(info, f"pv{script_id}")


def scripts_tab() -> None:
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        text = st.text_input("Search scripts", placeholder="e.g. follow-up NQM", icon=":material/search:",
                             key="lib_sc_q", width=420)
        include_old = st.toggle("Older versions", key="lib_sc_old")
        if st.button("Add script", icon=":material/post_add:", type="primary", disabled=not user.can("member")):
            add_script_dialog()
    st.caption("Approved email scripts tied to a cadence step are filled into Today's email batch (Subject and Body "
               "columns) for the mail-merge sheet.")
    found = _scripts(text, include_old)
    if found.empty:
        with card("no-scripts"):
            st.markdown("**No scripts yet.**")
            st.caption("Add your email and call scripts. Use fill-in fields like {first_name} and {organisation}.")
        return
    event = st.dataframe(found[["Title", "Kind", "Track", "Cadence step", "Status", "Version", "Subject"]],
                         hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="lib_sc_table", height=min(380, 35 * (len(found) + 1) + 3),
                         column_config={"Title": st.column_config.TextColumn(width="large", pinned=True)})
    rows = event.selection.rows if event and event.selection else []
    if rows:
        _script_detail(int(found.iloc[rows[0]]["id"]))
    else:
        st.caption("Tick a script to read it, preview it for a contact, edit or approve it.")


# ---------------------------------------------------------------- objections


@st.dialog("Objection and answer", width="large")
def objection_dialog(objection_id: int | None = None) -> None:
    c = _choices()
    current = None
    if objection_id:
        with session_scope() as session:
            row = session.get(Objection, objection_id)
            current = {"objection": row.objection, "response": row.response, "tags": row.tags or "",
                       "products": [p.id for p in row.products], "tracks": [t.id for t in row.tracks]}
    said = st.text_area("They say", value=current["objection"] if current else "", height=70,
                        placeholder="e.g. We don't have budget this year.", key=f"ob_said_{objection_id}")
    answer = st.text_area("We say", value=current["response"] if current else "", height=140,
                          key=f"ob_ans_{objection_id}")
    a, b = st.columns(2)
    products = a.multiselect("Products (empty = any)", [p for p, _ in c["products"]],
                             format_func=dict(c["products"]).get, default=current["products"] if current else [],
                             key=f"ob_prod_{objection_id}")
    tracks = b.multiselect("Tracks (empty = any)", [t for t, _ in c["tracks"]], format_func=dict(c["tracks"]).get,
                           default=current["tracks"] if current else [], key=f"ob_tr_{objection_id}")
    tags = st.text_input("Tags", value=current["tags"] if current else "", key=f"ob_tags_{objection_id}",
                         placeholder="e.g. budget, procurement")
    if st.button("Save", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                _, flags = save_objection(session, ObjectionData(said, answer, tags, products, tracks),
                                          session.get(User, user.id), objection_id)
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
            return
        _flash("Saved." + (" Check the wording: " + flags_message(flags) if flags else ""))


def objections_tab() -> None:
    c = _choices()
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        text = st.text_input("Search answers", placeholder="e.g. budget, GeM, faculty", icon=":material/search:",
                             key="lib_ob_q", width=360)
        product = st.selectbox("Product", [0] + [p for p, _ in c["products"]],
                               format_func=({0: "Any product"} | dict(c["products"])).get, key="lib_ob_prod",
                               width=240)
        track = st.selectbox("Track", [0] + [t for t, _ in c["tracks"]],
                             format_func=({0: "Any track"} | dict(c["tracks"])).get, key="lib_ob_track", width=260)
        if st.button("Add answer", icon=":material/add_comment:", type="primary", disabled=not user.can("member")):
            objection_dialog()
    found = _objections(text, product or None, track or None)
    if found.empty:
        st.caption("Nothing matches.")
        return
    for row in found.to_dict("records"):
        with card(f"ob-{row['id']}"):
            tags = [(row["Status"], _STATUS_TONE.get(row["Status"], "neutral"))]
            tags += [(p.strip(), "blue") for p in row["Products"].split(",") if p.strip()]
            tags += [t.strip() for t in row["Tags"].split(",") if t.strip()]
            html_block(f'<div class="qa-row-title">“{esc(row["They say"])}”</div>'
                       f'<div style="margin:6px 0 8px;white-space:pre-line">{esc(row["We say"])}</div>{chips(tags)}')
            with st.container(horizontal=True, gap="small"):
                if st.button("Edit", key=f"ob_edit_{row['id']}", type="tertiary", icon=":material/edit:",
                             disabled=not (user.can("member") and (row["Status"] == "Draft" or user.can("admin")))):
                    objection_dialog(int(row["id"]))
                if user.can("admin") and row["Status"] == "Draft":
                    if st.button("Approve", key=f"ob_ok_{row['id']}", type="tertiary", icon=":material/verified:"):
                        try:
                            with session_scope() as session:
                                set_objection_status(session, int(row["id"]), "approved", session.get(User, user.id))
                        except LibraryError as error:
                            st.error(str(error))
                        else:
                            _flash("Approved.")
                if user.can("admin"):
                    if st.button("Archive", key=f"ob_arch_{row['id']}", type="tertiary", icon=":material/archive:"):
                        with session_scope() as session:
                            set_objection_status(session, int(row["id"]), "archived", session.get(User, user.id))
                        _flash("Archived.")


# ---------------------------------------------------------------- page

page_header("Library", "Brochures, decks and links; email and call scripts; and answers to common objections. "
            "The approved version comes first.", eyebrow="Knowledge")
with session_scope() as session:
    counts = (len(documents_frame(session)), len(scripts_frame(session)), len(objections_frame(session)))
docs_tab, scripts_tab_, objections_tab_ = st.tabs([f"Documents · {counts[0]}", f"Scripts · {counts[1]}",
                                                   f"Objections · {counts[2]}"])
with docs_tab:
    documents_tab()
with scripts_tab_:
    scripts_tab()
with objections_tab_:
    objections_tab()
