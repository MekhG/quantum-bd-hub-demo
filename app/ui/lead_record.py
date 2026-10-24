"""The lead record page: one institution in one track — header, stage bar, activity feed and the side panel.

Opened from anywhere with /leads?lead=<id> (Today, search, alerts, Queries, Calendar, or 'Open lead record').
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Contact, Lead, OrgSignal, SignalType, Stage, Track, User
from app.services.activity_log import (
    LogEntry,
    LogError,
    delete_activity,
    finish_task,
    lead_timeline_activities,
    log_activity,
    strip_tags,
    update_activity,
)
from app.services.cadence import IST, STATUS_LABELS, start_cadence, stop_cadence
from app.services.editing import EditError, set_main_contact
from app.services.lead_queries import audit_frame, lead_detail
from app.services.library import documents_for_org
from app.services.procurement import deals_for_lead
from app.services.signals import SignalError, add_signal, hot_score, manual_types, recompute_scores, score_parts
from app.services.queries import QueryError, move_lead_track
from app.ui.common import CurrentUser
from app.ui.components import (
    avatar,
    card,
    channel_tile,
    chip,
    chips,
    day,
    esc,
    facts,
    html_block,
    initials,
    stage_stepper,
)
from app.ui.layout import nearest_time, time_options
from app.ui.quick_log import quick_log_dialog
from app.ui.task_dialog import new_task_dialog

FLASH = "edit_flash"
_OUTCOME_TONE = {"won": "green", "lost": "red", "nurture": "orange"}
_CADENCE_TONE = {"active": "blue", "awaiting_reply": "blue", "finished": "orange", "replied": "green",
                 "ghosted": "orange", "stopped": "neutral", "kept_open": "neutral"}


def _flash(kind: str, message: str, details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


def _local(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_convert(IST) if stamp.tzinfo else stamp


def _load(lead_id: int) -> dict | None:
    with session_scope() as session:
        detail = lead_detail(session, lead_id)
        if detail is None:
            return None
        org, lead = detail["org"], detail["lead"]
        path = [name for name, outcome in session.execute(
            select(Stage.name, Stage.outcome).where(Stage.is_active.is_(True)).order_by(Stage.sort_order))
            if outcome in ("open", "won")]
        main = session.get(Contact, lead.primary_contact_id) if lead.primary_contact_id else None
        verifier = session.get(User, main.verified_by_user_id) if main and main.verified_by_user_id else None
        tracks = [(t.id, t.name) for t in session.scalars(
            select(Track).where(Track.is_active.is_(True)).order_by(Track.sort_order))]
        changes = audit_frame(session, limit=200, entity_ids=[str(lead_id), str(org.id)])
        documents = documents_for_org(session, org.id)
        timeline = [
            {
                "id": a.id, "status": a.status, "type": a.activity_type.name, "channel": a.activity_type.channel,
                "title": a.title, "notes": strip_tags(a.notes), "outcome": a.outcome or "",
                "when": (a.scheduled_at if a.status == "planned" else a.occurred_at) or a.created_at,
                "owner": a.owner.name if a.owner else "", "owner_id": a.owner_user_id,
                "created_by": a.created_by_user_id, "cadence": a.cadence_step_id is not None,
                "with": ", ".join(p.contact.name for p in a.participants if p.contact is not None),
                "direction": a.direction,
            }
            for a in lead_timeline_activities(session, org.id)
        ]
        signals = session.execute(
            select(OrgSignal, SignalType.name, User.name).join(SignalType, SignalType.id == OrgSignal.signal_type_id)
            .outerjoin(User, User.id == OrgSignal.created_by_user_id).where(OrgSignal.org_id == org.id)
            .order_by(OrgSignal.observed_on.desc())).all()
        # Signals show in the timeline (read-only), dated the day they were seen.
        timeline += [{"id": f"sig-{sig.id}", "status": "done", "type": "Signal", "channel": "internal",
                      "title": f"Signal: {name}", "notes": sig.note or "", "outcome": "", "url": sig.source_url,
                      "when": datetime.combine(sig.observed_on, datetime.min.time().replace(hour=9), tzinfo=IST),
                      "owner": who or "", "owner_id": sig.created_by_user_id, "created_by": sig.created_by_user_id,
                      "cadence": False, "with": "", "direction": "internal"} for sig, name, who in signals]
        return {
            "score": lead.score, "hot": hot_score(session),
            "parts": score_parts(session, lead_id), "aliases": ", ".join(org.aliases or []),
            "signal_types": [(t.id, t.name) for t in manual_types(session)],
            "my_signals": [(sig.id, f"{name} · {sig.observed_on:%d %b %Y}", sig.created_by_user_id)
                           for sig, name, _ in signals],
            "lead_id": lead_id, "org_id": org.id, "name": org.name, "type": (org.org_type or "").replace("_", " "),
            "where": ", ".join(x for x in (org.city, org.state) if x), "aishe": org.aishe_code,
            "website": org.website, "university": org.affiliated_university, "management": org.management,
            "category": org.category, "area": org.area, "tier": org.tier,
            "track": lead.track.name, "stage": lead.stage.name, "outcome": lead.stage.outcome,
            "owner": lead.owner.name if lead.owner else "", "potential": lead.potential, "channel": lead.channel,
            "nqm": lead.nqm_flag, "notes": lead.notes, "interests": lead.interest_areas,
            "cadence_status": lead.cadence_status, "cadence_started": lead.cadence_started_on,
            "cadence_ended": lead.cadence_ended_on, "cadence_due": lead.cadence_due_on,
            "step_no": lead.current_step_no, "stage_since": lead.stage_changed_at,
            "main_id": lead.primary_contact_id, "path": path, "tracks": tracks,
            "main": None if main is None else {
                "name": main.name, "designation": main.designation, "email": main.email, "phone": main.phone,
                "verified": main.verified, "verified_by": verifier.name if verifier else "",
                "verified_on": main.verified_on, "bounced": main.email_status == "bounced"},
            "contact_choices": [(c.id, " · ".join(x for x in (c.name, c.designation, c.email) if x))
                                for c in org.contacts],
            "contacts": detail["contacts"], "deals": deals_for_lead(session, lead_id), "product_fit": detail["product_fit"],
            "other_tracks": detail["other_tracks"], "timeline": timeline, "documents": documents,
            "changes": changes[((changes["What"] == "Lead") & (changes["ID"] == str(lead_id)))
                               | ((changes["What"] == "Institution") & (changes["ID"] == str(org.id)))],
        }


def _header(r: dict, user: CurrentUser) -> None:
    can = user.can("member")
    with card("record-head"):
        info, actions = st.columns([3, 2.2], vertical_alignment="top")
        tags: list = [r["type"].title() if r["type"] else "", r["where"], r["tier"] or ""]
        if r["nqm"]:
            tags.append(("NQM applicant", "yellow"))
        tags.append((r["track"], "blue"))
        tags += [(f"Also in {t}", "neutral") for t in r["other_tracks"]]
        if r["outcome"] in ("lost", "nurture"):
            tags.append((r["stage"], _OUTCOME_TONE[r["outcome"]]))
        with info:
            html_block(
                f'<div class="qa-row" style="gap:16px;align-items:center">'
                f'<span class="qa-tile" aria-hidden="true" style="width:54px;height:54px;border-radius:14px;'
                f'background:rgba(139,90,43,0.14);font-family:Fraunces,serif;font-size:1.35rem">'
                f'{esc(initials(r["name"]))}</span><div class="qa-row-main" style="gap:8px">'
                f'<h1 class="qa-display" style="margin:0;padding:0;font-size:1.9rem">{esc(r["name"])}</h1>'
                f'{chips(tags)}</div></div>')
        with actions, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            if st.button("Log activity", icon=":material/add:", type="primary", key="rec_log", disabled=not can):
                quick_log_dialog(user, org_id=r["org_id"], org_label=r["name"], lead_id=r["lead_id"])
            if st.button("New task", key="rec_task", disabled=not can):
                new_task_dialog(user, org_id=r["org_id"], org_label=r["name"], lead_id=r["lead_id"])
            with st.popover("Move to track", disabled=not can):
                ids = [tid for tid, _ in r["tracks"]]
                target = st.selectbox("Track", ids, format_func=dict(r["tracks"]).get, key="rec_move")
                if st.button("Move", type="primary", key="rec_move_go"):
                    try:
                        with session_scope() as session:
                            note = move_lead_track(session, r["lead_id"], int(target), session.get(User, user.id))
                    except (QueryError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        _flash("success", note)
        html_block(stage_stepper(r["path"], r["stage"] if r["outcome"] in ("open", "won") else None))


def _note_box(r: dict, user: CurrentUser) -> None:
    with st.form("rec_note", clear_on_submit=True, border=False), \
            st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        text = st.text_input("Quick note", placeholder="Add a quick note — or use Log activity for a call, WhatsApp "
                             "or meeting", label_visibility="collapsed")
        if st.form_submit_button("Save note", disabled=not user.can("member")) and text.strip():
            try:
                with session_scope() as session:
                    log_activity(session, LogEntry(org_id=r["org_id"], lead_id=r["lead_id"],
                                                   type_name="Internal discussion", when=datetime.now(IST),
                                                   direction="internal", title="Note", notes=text.strip(),
                                                   owner_id=user.id), session.get(User, user.id))
            except (LogError, PermissionError) as error:
                st.error(str(error))
            else:
                _flash("success", "Note saved.")


def _edit_popover(item: dict, user: CurrentUser) -> None:
    may_edit = user.can("admin") or user.id in (item["owner_id"], item["created_by"])
    if item["type"] == "Stage change" or not may_edit:
        return
    with st.popover("Edit", icon=":material/edit:", type="tertiary"):
        new_title = st.text_input("Title", value=item["title"], key=f"tt_{item['id']}")
        new_outcome = st.text_input("Outcome", value=item["outcome"], key=f"to_{item['id']}")
        new_notes = st.text_area("Notes", value=item["notes"], key=f"tn_{item['id']}")
        local = _local(item["when"])
        new_day = st.date_input("Date", value=local.date(), key=f"td_{item['id']}", format="DD/MM/YYYY")
        times = time_options()
        new_time = datetime.strptime(st.selectbox("Time", times, index=times.index(nearest_time(local.time())),
                                                  key=f"tm_{item['id']}"), "%H:%M").time()
        if st.button("Save", key=f"ts_{item['id']}", type="primary"):
            try:
                with session_scope() as session:
                    update_activity(session, item["id"], session.get(User, user.id), title=new_title,
                                    notes=new_notes, outcome=new_outcome,
                                    when=datetime.combine(new_day, new_time, tzinfo=IST))
            except (LogError, PermissionError) as error:
                st.error(str(error))
            else:
                _flash("success", "Activity updated.")
        if not (item["status"] == "planned" and item["cadence"]):
            if st.button("Delete", key=f"tdel_{item['id']}", icon=":material/delete:"):
                try:
                    with session_scope() as session:
                        delete_activity(session, item["id"], session.get(User, user.id))
                except (LogError, PermissionError) as error:
                    st.error(str(error))
                else:
                    _flash("success", "Activity deleted.")
        else:
            st.caption("Cadence steps can't be deleted — Skip them on Today instead.")


def _who(item: dict) -> str:
    if item["direction"] == "inbound":
        return item["with"] or "They"
    return item["owner"] + (f" with {item['with']}" if item["with"] else "") if item["owner"] else item["with"]


def _planned(items: list[dict], user: CurrentUser) -> None:
    html_block('<div class="qa-eyebrow" style="margin-top:0.3rem">Planned</div>')
    for item in items:
        local = _local(item["when"])
        with card(f"plan-{item['id']}", "info"):
            html_block(
                f'<div class="qa-row">{channel_tile(item["channel"], 38)}<div class="qa-row-main">'
                f'<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px">'
                f'<span class="qa-row-title">{esc(item["title"])}</span>'
                f'<b>{local.strftime("%a %d %b · %I:%M %p").replace(" 0", " ")}</b></div>'
                f'<div class="qa-muted">{esc(_who(item))}</div>'
                + (f'<div class="qa-small qa-muted">{esc(item["notes"].splitlines()[-1])}</div>' if item["notes"]
                   else "") + "</div></div>")
            with st.container(horizontal=True, gap="small", key=f"qa-actions-plan-{item['id']}"):
                if st.button("Mark done", icon=":material/check:", key=f"pdone_{item['id']}",
                             disabled=not user.can("member")):
                    try:
                        with session_scope() as session:
                            messages = finish_task(session, item["id"], session.get(User, user.id))
                    except (LogError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        _flash("success", " ".join(["Done.", *messages]))
                _edit_popover(item, user)


def _feed(items: list[dict], user: CurrentUser) -> None:
    html_block('<div class="qa-eyebrow" style="margin-top:0.6rem">Earlier</div>')
    if not items:
        st.caption("Nothing logged yet. Use **Log activity** to record a call, WhatsApp chat or meeting.")
        return
    for item in items:
        local = _local(item["when"])
        when = local.strftime("%d %b, %I:%M %p").replace(" 0", " ").lstrip("0")
        if item["type"] == "Stage change":
            html_block(f'<div class="qa-row" style="gap:10px;align-items:center;padding:4px 0 4px 46px">'
                       f'{chip(item["title"], "green")}<span class="qa-small qa-muted">{esc(item["owner"])} · '
                       f'{when}</span></div>')
            continue
        if item["type"] == "Signal":
            link = (f' · <a href="{esc(item["url"])}" target="_blank" rel="noopener" style="color:inherit">source</a>'
                    if item.get("url") else "")
            html_block(f'<div class="qa-row" style="gap:10px;align-items:flex-start;padding:4px 0 4px 46px">'
                       f'{chip(item["title"], "yellow")}<span class="qa-small qa-muted">'
                       f'{esc(item["notes"][:160])}{" · " if item["notes"] else ""}{local.strftime("%d %b %Y")}'
                       f'{" · " + esc(item["owner"]) if item["owner"] else ""}{link}</span></div>')
            continue
        status = {"cancelled": ("Cancelled", "neutral"), "missed": ("Missed", "orange")}.get(item["status"])
        direction = "Received · " if item["direction"] == "inbound" else ""
        with st.container(key=f"qa-feed-{item['id']}"):
            body, edit = st.columns([12, 1.6], vertical_alignment="top")
            with body:
                html_block(
                    f'<div class="qa-row" style="gap:12px">{channel_tile(item["channel"], 34)}'
                    f'<div class="qa-row-main"><div style="display:flex;flex-wrap:wrap;justify-content:space-between;'
                    f'gap:8px"><span><b>{esc(item["title"])}</b> <span class="qa-muted">· {esc(direction)}'
                    f'{esc(_who(item))}</span></span><span class="qa-small qa-muted">{when}</span></div>'
                    + (f'<div><i>{esc(item["outcome"])}</i></div>' if item["outcome"] else "")
                    + (f'<div style="white-space:pre-line">{esc(item["notes"])}</div>' if item["notes"] else "")
                    + (f'<div>{chip(status[0], status[1])}</div>' if status else "")
                    + "</div></div>")
            with edit:
                _edit_popover(item, user)


def _main_contact(r: dict, user: CurrentUser) -> None:
    with card("rec-contact"):
        top, switch = st.columns([2, 1.2], vertical_alignment="center")
        top.markdown("**Main contact**")
        with switch, st.popover("Switch", type="tertiary", disabled=not user.can("member")):
            ids = [cid for cid, _ in r["contact_choices"]]
            chosen = st.selectbox("Cadence emails and calls go to this person", ids,
                                  format_func=dict(r["contact_choices"]).get,
                                  index=ids.index(r["main_id"]) if r["main_id"] in ids else None,
                                  placeholder="Not set", key="rec_main")
            if st.button("Save", type="primary", key="rec_main_save", disabled=chosen in (None, r["main_id"])):
                try:
                    with session_scope() as session:
                        set_main_contact(session, r["lead_id"], int(chosen), session.get(User, user.id))
                except EditError as error:
                    st.error(str(error))
                else:
                    _flash("success", "Main contact updated.")
        main = r["main"]
        if main is None:
            st.caption("Not set. Pick one with Switch — cadences need a verified main contact.")
            return
        lines = []
        if main["email"]:
            lines.append(f'<a href="mailto:{esc(main["email"])}" style="color:inherit">{esc(main["email"])}</a>')
        if main["phone"]:
            lines.append(f'<a href="tel:{esc(main["phone"].replace(" ", ""))}" style="color:inherit">'
                         f'{esc(main["phone"])}</a>')
        if main["bounced"]:
            state = chip("Email bounced", "orange")
        elif main["verified"]:
            by = f" by {main['verified_by']}" if main["verified_by"] else ""
            on = f" · {day(main['verified_on'])}" if main["verified_on"] else ""
            state = chip(f"Verified{by}{on}", "green")
        else:
            state = chip("Not verified", "orange")
        html_block(f'<div class="qa-row" style="gap:12px;align-items:center">{avatar(main["name"], 40)}'
                   f'<div class="qa-row-main"><b>{esc(main["name"])}</b><span class="qa-small qa-muted">'
                   f'{esc(main["designation"] or "")}</span></div></div>'
                   f'<div class="qa-small" style="display:flex;flex-direction:column;gap:4px;margin-top:10px">'
                   f'{"".join(f"<div>{line}</div>" for line in lines)}<div>{state}</div></div>')


def _cadence(r: dict, user: CurrentUser) -> None:
    status = r["cadence_status"]
    with card("rec-cadence"):
        st.markdown("**Cadence**")
        label = STATUS_LABELS.get(status, "Not started") if status else "Not started"
        if status in ("active", "awaiting_reply"):
            detail = f"Step {r['step_no']} · next due {day(r['cadence_due'])}" if r["cadence_due"] \
                else f"Step {r['step_no'] or 1}"
        elif status in ("replied", "stopped", "ghosted") and r["cadence_ended"]:
            detail = f"Ended {day(r['cadence_ended'])}" + (f" after step {r['step_no']}"
                                                                       if r["step_no"] else "")
        elif status == "finished":
            detail = "All steps done, no reply — decide on Today"
        else:
            detail = "Needs a verified main contact to start."
        html_block(f'<div class="qa-row" style="gap:8px;align-items:center">'
                   f'{chip(label, _CADENCE_TONE.get(status, "neutral"), strong=True)}'
                   f'<span class="qa-small qa-muted">{esc(detail)}</span></div>')
        if status == "replied":
            st.caption("Follow-ups stop automatically when they reply. Plan next steps with New task.")
        if status in ("active", "awaiting_reply", "finished"):
            if st.button("Stop cadence", icon=":material/stop:", key="rec_stop", disabled=not user.can("member")):
                with session_scope() as session:
                    cancelled = stop_cadence(session, session.get(Lead, r["lead_id"]), session.get(User, user.id))
                _flash("success", f"Cadence stopped ({cancelled} planned step(s) cancelled).")
        elif status not in ("replied",) and r["outcome"] == "open":
            if st.button("Start cadence", icon=":material/play_arrow:", key="rec_start",
                         disabled=not user.can("member")):
                with session_scope() as session:
                    result = start_cadence(session, [r["lead_id"]], session.get(User, user.id))
                _flash("success" if result.started else "warning", result.summary(),
                       [f"{name}: {reason}" for name, reason in result.skipped])


def _signals(r: dict, user: CurrentUser) -> None:
    """The lead score and what makes it up, plus adding a signal and the names the news uses."""
    from app.services.news import rematch, save_org_aliases
    from app.services.signals import remove_signal

    can = user.can("member")
    hot = r["score"] >= r["hot"] and r["outcome"] in ("open", "nurture")
    with card("rec-signals"):
        label = ("Hot · " if hot else "") + f"Score {r['score']}"
        html_block(f'<div style="display:flex;justify-content:space-between;align-items:center">'
                   f'<b>Signals</b>{chip(label, "yellow" if hot else "neutral", strong=True)}</div>')
        counting = [p for p in r["parts"] if p.points > 0]
        if not counting:
            st.caption("No signals yet. Add one when you see news about this institution — an NQM lab, a tender, a "
                       "new centre — or save it from News & signals.")
        rows = []
        for part in counting:
            when = part.when.strftime("%d %b %Y") if part.when else ""
            source = (f' · <a href="{esc(part.source)}" target="_blank" rel="noopener" style="color:inherit">source</a>'
                      if part.source else "")
            detail = " · ".join(x for x in ("automatic" if part.auto else "", when) if x)
            rows.append(f'<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0">'
                        f'<div style="display:flex;flex-direction:column"><span>{esc(part.signal)}</span>'
                        f'<span class="qa-small qa-muted">{esc(detail)}{source if detail else source[3:]}</span>'
                        f'</div><b class="qa-mono">+{part.points}</b></div>')
        if rows:
            html_block("".join(rows))
        with st.container(horizontal=True, gap="small"):
            with st.popover("Add signal", icon=":material/add:", disabled=not can):
                ids = [tid for tid, _ in r["signal_types"]]
                kind = st.selectbox("Signal", ids, format_func=dict(r["signal_types"]).get, key="sig_type")
                seen = st.date_input("Seen on", value=datetime.now(IST).date(), max_value=datetime.now(IST).date(),
                                     format="DD/MM/YYYY", key="sig_day")
                url = st.text_input("Source link (optional)", placeholder="https://…", key="sig_url")
                note = st.text_input("Note (optional)", placeholder="e.g. Lab approved in the second NQM call",
                                     key="sig_note")
                if st.button("Save signal", type="primary", key="sig_save"):
                    try:
                        with session_scope() as session:
                            actor = session.get(User, user.id)
                            add_signal(session, org_id=r["org_id"], type_id=int(kind), observed_on=seen, user=actor,
                                       source_url=url, note=note)
                            recompute_scores(session, org_ids=[r["org_id"]])
                    except (SignalError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        _flash("success", "Signal saved; the score is updated for every lead of this institution.")
            removable = [(sid, label) for sid, label, by in r["my_signals"] if user.can("admin") or by == user.id]
            if removable:
                with st.popover("Remove", type="tertiary", disabled=not can):
                    sid = st.selectbox("Signal", [s for s, _ in removable], format_func=dict(removable).get,
                                       key="sig_remove")
                    if st.button("Remove signal", key="sig_remove_go"):
                        try:
                            with session_scope() as session:
                                label = remove_signal(session, int(sid), session.get(User, user.id))
                        except (SignalError, PermissionError) as error:
                            st.error(str(error))
                        else:
                            _flash("success", f"Removed: {label}.")
            with st.popover("Names in news", type="tertiary", disabled=not can):
                st.caption("Other names the news uses for this institution, separated by commas — e.g. "
                           "“IIT Bombay, IITB”. Used to spot it in headlines.")
                text = st.text_input("Also known as", value=r["aliases"], key="sig_aliases")
                if st.button("Save names", type="primary", key="sig_aliases_go"):
                    with session_scope() as session:
                        save_org_aliases(session, r["org_id"], text, session.get(User, user.id))
                        found = rematch(session)
                    _flash("success", "Names saved." + (f" {found} recent news item(s) re-checked." if found else ""))


def _details(r: dict) -> None:
    with card("rec-details"):
        st.markdown("**Details**")
        since = _local(r["stage_since"]) if r["stage_since"] is not None else None
        days = (datetime.now(IST).date() - since.date()).days if since is not None else None
        website = (f'<a href="{esc(r["website"])}" target="_blank" rel="noopener" style="color:inherit">'
                   f'{esc(r["website"].replace("https://", "").replace("http://", ""))}</a>') if r["website"] else ""
        html_block(facts([
            ("AISHE code", f'<span class="qa-mono">{esc(r["aishe"])}</span>' if r["aishe"] else ""),
            ("Management", esc(r["management"] or "")),
            ("Owner", esc(r["owner"])),
            ("Potential", f'<b>{esc(r["potential"])}</b>' if r["potential"] else ""),
            ("Channel", esc(r["channel"] or "")),
            ("Area", esc(r["area"] or "")),
            ("Category", esc(r["category"] or "")),
            ("University", esc(r["university"] or "")),
            ("Website", website),
            ("In stage since", f"{day(since)} · {days} day{'s' if days != 1 else ''}"
             if since is not None else ""),
        ]))
        tags = [(p, "blue") for p in r["product_fit"]]
        tags += [p.strip() for p in (r["interests"] or "").split(";") if p.strip()]
        if tags:
            html_block(f'<div style="margin-top:10px">{chips(tags)}</div>')
        if r["notes"]:
            with st.expander("Notes", icon=":material/sticky_note_2:"):
                st.text(r["notes"])


def render_lead_record(lead_id: int, user: CurrentUser) -> None:
    r = _load(lead_id)
    if r is None:
        st.warning("That lead no longer exists.")
        st.page_link("views/leads.py", label="All leads", icon=":material/arrow_back:")
        return
    # Clearing ?lead= (rather than linking to the same page) is what takes you back to the list.
    if st.button("All leads", icon=":material/arrow_back:", type="tertiary", key="rec_back"):
        del st.query_params["lead"]
        st.rerun()
    _header(r, user)
    main, side = st.columns([2.2, 1], gap="large")
    with main:
        planned = [t for t in r["timeline"] if t["status"] == "planned"]
        earlier = sorted((t for t in r["timeline"] if t["status"] != "planned"),
                         key=lambda t: _local(t["when"]), reverse=True)
        activity_tab, contacts_tab, deals_tab, docs_tab, changes_tab = st.tabs(
            ["Activity", f"Contacts · {len(r['contacts'])}", f"Deals · {len(r['deals'])}",
             f"Documents · {len(r['documents'])}", f"Changes · {len(r['changes'])}"])
        with activity_tab:
            _note_box(r, user)
            if planned:
                _planned(planned, user)
            _feed(earlier, user)
        with contacts_tab:
            if r["contacts"].empty:
                st.caption("No contacts yet. Add one with **New → Contact** at the top.")
            else:
                st.dataframe(r["contacts"], hide_index=True, width="stretch",
                             column_config={"Email": st.column_config.TextColumn(width="medium")})
        with deals_tab:
            from app.ui.deals import render_deals  # local import: only loaded when a lead record is open

            render_deals(r["lead_id"], user)
        with docs_tab:
            if r["documents"].empty:
                st.caption("Nothing recorded as shared yet. When you log an email or meeting, pick the brochure or "
                           "deck under “Documents shared”.")
            else:
                st.dataframe(r["documents"], hide_index=True, width="stretch",
                             column_config={"When": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
                                            "Link": st.column_config.LinkColumn(display_text="Open")})
        with changes_tab:
            if r["changes"].empty:
                st.caption("No edits yet.")
            else:
                st.dataframe(r["changes"].drop(columns=["ID"]), hide_index=True, width="stretch",
                             column_config={"When (IST)": st.column_config.DatetimeColumn(
                                 format="DD MMM YYYY, HH:mm")})
    with side:
        _main_contact(r, user)
        _signals(r, user)
        _cadence(r, user)
        _details(r)
