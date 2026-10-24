"""News & signals: free news searches and feeds matched to our leads, the signals saved against institutions, and
adding signals by hand, from a pasted list or from a sheet."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.models import User
from app.services.cadence import today_ist
from app.services.lead_queries import frame_to_xlsx
from app.services.news import (
    NewsError,
    due_sources,
    fetch_news,
    news_items,
    news_topics,
    save_as_signal,
    save_to_library,
    set_status,
)
from app.services.content import idea_from_news
from app.services.signals import (
    SignalError,
    hot_leads,
    hot_score,
    lead_orgs,
    manual_types,
    match_names,
    read_signal_sheet,
    remove_signal,
    save_signal_rows,
    signal_sheet_template,
    signals_frame,
)
from app.ui.common import require_user
from app.ui.components import card, chip, chips, esc, html_block
from app.ui.layout import page_header

user = require_user()
FLASH = "edit_flash"
PAGE = 30
SHOW = {"New & saved": ("new", "saved"), "Saved": ("saved",), "Used for signals": ("signal",),
        "Not relevant": ("dismissed",)}


def _done(text: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, text, [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def _choices() -> tuple[dict[str, int], list[tuple[int, str]], list[str]]:
    """Lead institutions as 'label → id' (labels made unique), signal types, topics."""
    with session_scope() as session:
        orgs = lead_orgs(session)
        seen: dict[str, int] = {}
        for _, name, _ in orgs:
            seen[name] = seen.get(name, 0) + 1
        labels = {(name if seen[name] == 1 else f"{name} · #{oid}"): oid for oid, name, _ in orgs}
        return labels, [(t.id, t.name) for t in manual_types(session)], news_topics(session)


@st.cache_data(ttl=60, show_spinner=False)
def _items(statuses: tuple[str, ...], topics: tuple[str, ...], leads_only: bool, text: str) -> list[dict]:
    with session_scope() as session:
        return news_items(session, statuses=statuses, topics=topics, leads_only=leads_only, text=text, limit=300)


@st.cache_data(ttl=60, show_spinner=False)
def _signals(include_faded: bool) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    with session_scope() as session:
        return signals_frame(session, include_faded=include_faded), hot_leads(session, limit=15), hot_score(session)


def _fetch(force: bool) -> None:
    try:
        with st.spinner("Checking the news feeds…"), session_scope() as session:
            result = fetch_news(session, force=force)
    except Exception as error:  # noqa: BLE001 — a feed problem must never break the page
        st.warning(f"Couldn't check the news just now ({type(error).__name__}). Try Refresh in a minute.")
        return
    st.session_state["news_checked"] = datetime.now(timezone.utc)
    st.cache_data.clear()
    if force or result.new_items or result.errors:
        st.toast(result.summary(), icon=":material/newspaper:")


def _maybe_fetch() -> None:
    # Fetch on open when the last fetch is older than the refresh interval (Configuration → News & signals),
    # at most once every 10 minutes per browser session.
    last = st.session_state.get("news_checked")
    if last and datetime.now(timezone.utc) - last < timedelta(minutes=10):
        return
    with session_scope() as session:
        due = bool(due_sources(session))
    if due:
        _fetch(force=False)
    else:
        st.session_state["news_checked"] = datetime.now(timezone.utc)


# ---------------------------------------------------------------- dialogs


@st.dialog("Save as signal", width="large")
def signal_dialog(item: dict) -> None:
    labels, types, _ = _choices()
    by_id = {oid: label for label, oid in labels.items()}
    st.markdown(f"**{item['title']}**")
    st.caption(" · ".join(x for x in (item["publisher"], item["published"].strftime("%d %b %Y")
                                       if item["published"] else "") if x))
    picked = st.multiselect("Institutions", list(labels), default=[by_id[m[0]] for m in item["matched"]
                                                                     if m[0] in by_id],
                            placeholder="Search our leads by name",
                            help="Only institutions that are leads are listed. The signal counts for every lead "
                                 "the institution has.")
    ids = [tid for tid, _ in types]
    suggested = item["suggested_type_id"]
    kind = st.selectbox("Signal", ids, format_func=dict(types).get,
                        index=ids.index(suggested) if suggested in ids else 0)
    published = item["published"].date() if item["published"] else today_ist()
    seen = st.date_input("Seen on", value=min(published, today_ist()), max_value=today_ist(), format="DD/MM/YYYY")
    note = st.text_input("Note (optional)", placeholder="Leave blank to use the headline")
    if st.button("Save signal", type="primary", icon=":material/bolt:", disabled=not picked):
        try:
            with session_scope() as session:
                added = save_as_signal(session, item["id"], org_ids=[labels[p] for p in picked], type_id=int(kind),
                                       observed_on=seen, user=session.get(User, user.id), note=note or None)
        except (NewsError, SignalError, PermissionError) as error:
            st.error(str(error))
            return
        _done(f"Signal saved for {added} institution(s); their lead scores are updated.")


# ---------------------------------------------------------------- news cards


def news_card(item: dict) -> None:
    can = user.can("member")
    key = item["id"]
    matched = item["matched"]
    with card(f"news-{key}", "info" if matched and item["status"] == "new" else "plain"):
        when = item["published"].strftime("%d %b %Y") if item["published"] else ""
        tags = [(item["topic"], "blue")] if item["topic"] else []
        if item["suggested"]:
            tags.append((f"Looks like: {item['suggested']}", "yellow"))
        if item["status"] == "saved":
            tags.append(("Saved", "green"))
        elif item["status"] == "signal":
            tags.append(("Used for a signal", "green"))
        if item["in_library"]:
            tags.append(("In the Library", "neutral"))
        html_block(
            f'<div class="qa-row-main" style="gap:4px">'
            f'<a href="{esc(item["url"])}" target="_blank" rel="noopener" class="qa-row-title" '
            f'style="color:inherit;text-decoration:none">{esc(item["title"])}</a>'
            f'<div class="qa-small qa-muted">{esc(" · ".join(x for x in (item["publisher"], when) if x))}</div>'
            + (f'<div class="qa-small" style="margin-top:2px">{esc(item["summary"][:280])}'
               f'{"…" if len(item["summary"]) > 280 else ""}</div>' if item["summary"] else "")
            + f'<div style="margin-top:4px">{chips(tags)}</div>'
            + (f'<div class="qa-small qa-muted" style="margin-top:6px">Mentions our lead'
               f'{"s" if len(matched) > 1 else ""}:</div>' if matched else "") + "</div>")
        if matched:
            # Links to the lead records; the label sits in the markup above so the links start at the left edge.
            with st.container(horizontal=True, gap="small", vertical_alignment="center", key=f"qa-matched-{key}"):
                for _, name, lead_id in matched[:4]:
                    st.page_link("views/leads.py", label=name, icon=":material/target:",
                                 query_params={"lead": int(lead_id)})
        with st.container(horizontal=True, gap="small", key=f"qa-newsbar-{key}"):
            if item["status"] == "dismissed":
                if st.button("Restore", key=f"nr_{key}", disabled=not can, icon=":material/undo:"):
                    with session_scope() as session:
                        set_status(session, key, "new", session.get(User, user.id))
                    _done("Restored.")
                return
            if st.button("Save as signal", key=f"ns_{key}", icon=":material/bolt:", disabled=not can,
                         type="primary" if matched and item["status"] != "signal" else "secondary"):
                signal_dialog(item)
            if st.button("Save to Library", key=f"nl_{key}", icon=":material/library_add:",
                         disabled=not can or item["in_library"]):
                try:
                    with session_scope() as session:
                        save_to_library(session, key, session.get(User, user.id))
                except (NewsError, PermissionError) as error:
                    st.error(str(error))
                else:
                    _done("Added to the Library as a News clipping (a draft until approved).")
            if st.button("Make a post", key=f"np_{key}", icon=":material/campaign:", disabled=not can,
                         help="Starts a LinkedIn/Instagram idea on the Content page with this article linked."):
                with session_scope() as session:
                    post_id = idea_from_news(session, key, session.get(User, user.id)).id
                st.cache_data.clear()
                st.switch_page("views/content.py", query_params={"post": post_id})
            if item["status"] == "new":
                if st.button("Bookmark", key=f"nb_{key}", icon=":material/bookmark:", type="tertiary",
                             disabled=not can):
                    with session_scope() as session:
                        set_status(session, key, "saved", session.get(User, user.id))
                    _done("Bookmarked — find it under Show: Saved.")
            if st.button("Not relevant", key=f"nd_{key}", icon=":material/close:", type="tertiary",
                         disabled=not can):
                with session_scope() as session:
                    set_status(session, key, "dismissed", session.get(User, user.id))
                st.cache_data.clear()
                st.rerun()


def news_tab() -> None:
    labels, _, topics = _choices()
    with st.container(horizontal=True, gap="small", vertical_alignment="bottom"):
        text = st.text_input("Search the news", placeholder="e.g. tender, Kerala, AICTE", icon=":material/search:",
                             key="news_q")
        chosen = st.multiselect("Topic", topics, placeholder="All topics", key="news_topics")
        show = st.selectbox("Show", list(SHOW), key="news_show")
    leads_only = st.toggle("Only news that mentions our leads", key="news_leads_only")
    items = _items(SHOW[show], tuple(chosen), leads_only, (text or "").strip())
    mentioning = sum(1 for i in items if i["matched"])
    st.caption(f"{len(items)} item{'s' if len(items) != 1 else ''}" + (f" · {mentioning} mention our leads"
                                                                       if mentioning and not leads_only else ""))
    if not items:
        with card("news-empty"):
            st.markdown("**Nothing here yet.**")
            st.caption("Press Refresh to check the feeds now. The searches are set in Configuration → News & "
                       "signals; add aliases on a lead (Signals → Names in news) so headlines that use a short name "
                       "are matched too.")
        return
    limit = st.session_state.get("news_limit", PAGE)
    for item in items[:limit]:
        news_card(item)
    if len(items) > limit and st.button(f"Show more ({len(items) - limit} left)", key="news_more"):
        st.session_state["news_limit"] = limit + PAGE
        st.rerun()


# ---------------------------------------------------------------- signals


def signals_tab() -> None:
    include_faded = st.toggle("Include signals that no longer count", key="sig_faded",
                              help="Signals fade to zero over their type's months (Configuration → News & signals).")
    signals, hot, threshold = _signals(include_faded)
    with card("hot-leads"):
        html_block(f'<div style="display:flex;justify-content:space-between;align-items:center"><b>Hot leads</b>'
                   f'{chip(f"score {threshold} or more", "yellow")}</div>')
        if hot.empty:
            st.caption("No lead has reached the hot score yet. Scores add up signals and automatic points (open "
                       "quote, recent reply, demo, NQM flag).")
        else:
            st.caption("Open leads with the highest scores — they also rise to the top of Today.")
            with st.container(horizontal=True, gap="small"):
                for row in hot.to_dict("records"):
                    st.page_link("views/leads.py", label=f"{row['Institution']} · {row['Score']}",
                                 query_params={"lead": int(row["lead_id"])}, icon=":material/local_fire_department:",
                                 help=f"{row['Track']} · {row['Stage']}" + (f" · {row['Owner']}" if row["Owner"]
                                                                             else ""))
    if signals.empty:
        st.caption("No signals saved yet. Save one from a news item, from a lead's Signals card, or under "
                   "Add signals.")
        return
    columns = ["Institution", "Signal", "Points now", "Seen on", "Counts until", "Source", "Note", "Added by"]
    event = st.dataframe(
        signals[columns], hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
        key="sig_table", height=min(480, 35 * (len(signals) + 1) + 3),
        column_config={
            "Points now": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                          max_value=max(int(signals["Points now"].max()), 1)),
            "Seen on": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Counts until": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Source": st.column_config.LinkColumn(display_text="Open"),
            "Note": st.column_config.TextColumn(width="medium"),
        })
    picked = signals.iloc[event.selection.rows] if event and event.selection else signals.iloc[0:0]
    mine = picked if user.can("admin") else picked[picked["created_by"] == user.id]
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.download_button("Download (Excel)", data=frame_to_xlsx(signals[columns], "Signals"),
                           file_name=f"signals-{date.today().isoformat()}.xlsx", icon=":material/download:")
        if st.button(f"Remove ({len(mine)})" if len(mine) else "Remove", icon=":material/delete:",
                     disabled=mine.empty or not user.can("member"), key="sig_remove_many"):
            with session_scope() as session:
                actor = session.get(User, user.id)
                for signal_id in mine["id"].astype(int):
                    remove_signal(session, int(signal_id), actor)
            _done(f"Removed {len(mine)} signal(s); scores updated.")
        if len(picked) > len(mine):
            st.caption("You can remove only signals you added (an admin can remove any).")
        else:
            st.caption("Tick rows at the left edge to remove them.")


# ---------------------------------------------------------------- adding signals


def _review(frame: pd.DataFrame, labels: dict[str, int], types: list[tuple[int, str]]) -> None:
    """The match review: fix any institution the hub matched wrongly, untick rows to leave out, save."""
    type_names = [name for _, name in types]
    edited = st.data_editor(
        frame, hide_index=True, width="stretch", key="sig_review_grid",
        disabled=["As written", "Match"],
        column_config={
            "Include": st.column_config.CheckboxColumn(width="small"),
            "Institution": st.column_config.SelectboxColumn(options=list(labels), width="large",
                                                            help="Pick the right lead if the match is wrong."),
            "Signal": st.column_config.SelectboxColumn(options=type_names, required=True),
            "Seen on": st.column_config.DateColumn(format="DD/MM/YYYY", max_value=today_ist(), required=True),
            "Source link": st.column_config.LinkColumn(),
        })
    ready = edited[edited["Include"] & edited["Institution"].notna() & (edited["Institution"] != "")]
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        if st.button(f"Save {len(ready)} signal(s)", type="primary", icon=":material/save:", disabled=ready.empty,
                     key="sig_review_save"):
            type_ids = {name: tid for tid, name in types}
            rows = [{"org_id": labels[r["Institution"]], "type_id": type_ids[r["Signal"]],
                     "observed_on": pd.Timestamp(r["Seen on"]).date(), "source_url": r["Source link"] or None,
                     "note": r["Note"] or None, "label": r["Institution"]} for r in ready.to_dict("records")]
            with session_scope() as session:
                added, already, problems = save_signal_rows(session, rows, session.get(User, user.id))
            st.session_state.pop("sig_review", None)
            text = f"{added} signal(s) saved" + (f", {already} were already there" if already else "") + "."
            st.session_state[FLASH] = ("warning" if problems else "success", text, problems)
            st.cache_data.clear()
            st.rerun()
        if st.button("Start over", type="tertiary", key="sig_review_reset"):
            st.session_state.pop("sig_review", None)
            st.rerun()
        missing = int((edited["Match"] == "Not found").sum())
        if missing:
            st.caption(f"{missing} name(s) aren't leads yet — add them as leads first, or pick a lead by hand.")


def add_tab() -> None:
    labels, types, _ = _choices()
    if not user.can("member"):
        st.info("Viewers can see signals but not add them.")
        return
    if "sig_review" in st.session_state:
        st.markdown("**Check the matches**")
        st.caption("Exact = the name, short form or an alias matched. Close = a near spelling — check it. "
                   "Not found = not a lead yet.")
        _review(st.session_state["sig_review"], labels, types)
        return
    one, many = st.columns(2, gap="large")
    type_ids = [tid for tid, _ in types]
    with one, card("sig-one"):
        st.markdown("**One institution**")
        with st.form("sig_one_form", clear_on_submit=True, border=False):
            org = st.selectbox("Institution", list(labels), index=None, placeholder="Search our leads by name")
            kind = st.selectbox("Signal", type_ids, format_func=dict(types).get)
            seen = st.date_input("Seen on", value=today_ist(), max_value=today_ist(), format="DD/MM/YYYY")
            url = st.text_input("Source link (optional)", placeholder="https://…")
            note = st.text_input("Note (optional)")
            if st.form_submit_button("Save signal", type="primary", icon=":material/bolt:"):
                if not org:
                    st.error("Pick an institution.")
                else:
                    with session_scope() as session:
                        added, _, problems = save_signal_rows(
                            session, [{"org_id": labels[org], "type_id": int(kind), "observed_on": seen,
                                       "source_url": url, "note": note, "label": org}], session.get(User, user.id))
                    if problems:
                        st.error(problems[0])
                    else:
                        _done("Signal saved; the lead score is updated." if added else "That signal was already "
                              "saved.")
    with many, card("sig-many"):
        st.markdown("**A list of institutions**")
        st.caption("For example, the institutions named in a PIB release. Paste one name per line, or upload a "
                   "sheet. You'll check the matches before anything is saved.")
        kind = st.selectbox("Signal", type_ids, format_func=dict(types).get, key="sig_many_type")
        c1, c2 = st.columns(2)
        seen = c1.date_input("Seen on", value=today_ist(), max_value=today_ist(), format="DD/MM/YYYY",
                             key="sig_many_day")
        url = c2.text_input("Source link (optional)", placeholder="https://…", key="sig_many_url")
        note = st.text_input("Note (optional)", key="sig_many_note")
        pasted, uploaded = st.tabs(["Paste names", "Upload a sheet"])
        with pasted:
            text = st.text_area("Institutions, one per line", height=150, key="sig_many_text",
                                placeholder="Omega University\nIIT Kalpaka\n…")
            # Never disabled: a text area only reports its text when it loses focus, so a button that waits for the
            # text would swallow the first click.
            go_paste = st.button("Find these institutions", key="sig_many_find")
        with uploaded:
            file = st.file_uploader("Sheet (.xlsx or .csv, up to 2 MB)", type=["xlsx", "csv"], key="sig_many_file")
            st.download_button("Download the template", data=signal_sheet_template(),
                               file_name="signals-template.xlsx", icon=":material/download:", type="tertiary")
            st.caption("Columns: Institution (required), Signal, Seen on, Source link, Note. Blank cells use the "
                       "choices above.")
            go_file = st.button("Read the sheet", key="sig_many_read")
        sheet = None
        if go_paste and not text.strip():
            st.warning("Paste at least one institution name.")
            go_paste = False
        if go_file and file is None:
            st.warning("Choose a sheet to upload first.")
        if go_file and file is not None:
            try:
                sheet = read_signal_sheet(file.getvalue(), file.name)
            except SignalError as error:
                st.error(str(error))
        if go_paste or sheet is not None:
            names = sheet["Institution"].tolist() if sheet is not None else text.splitlines()
            with session_scope() as session:
                matches = match_names(session, names)
            by_id = {oid: label for label, oid in labels.items()}
            default_type = dict(types).get(int(kind))
            valid_types = {name for _, name in types}
            rows = []
            for i, m in enumerate(matches):
                extra = sheet.iloc[i].to_dict() if sheet is not None else {}
                when = pd.to_datetime(extra.get("Seen on") or None, errors="coerce", dayfirst=True)
                rows.append({
                    "Include": m.org_id is not None, "As written": m.pasted, "Match": m.how,
                    "Institution": by_id.get(m.org_id),
                    "Signal": extra.get("Signal") if extra.get("Signal") in valid_types else default_type,
                    "Seen on": min(when.date(), today_ist()) if not pd.isna(when) else seen,
                    "Source link": (extra.get("Source link") or url or "").strip(),
                    "Note": (extra.get("Note") or note or "").strip()})
            if not rows:
                st.error("No names found.")
            else:
                st.session_state["sig_review"] = pd.DataFrame(rows)
                st.rerun()


# ---------------------------------------------------------------- page

head, tools = st.columns([4, 1.2], vertical_alignment="bottom")
with head:
    page_header("News & signals", "Free news searches and feeds, matched to our leads. Save what matters as a "
                "signal — it raises the lead score, so the hottest leads rise to the top of Today.",
                eyebrow="Knowledge")
with tools:
    refresh = st.button("Refresh", icon=":material/refresh:", width="stretch",
                        help="Check every news source now (otherwise they're checked every few hours).")
if refresh:
    _fetch(force=True)
else:
    _maybe_fetch()

news, signals, add = st.tabs(["News", "Signals & hot leads", "Add signals"])
with news:
    news_tab()
with signals:
    signals_tab()
with add:
    add_tab()
