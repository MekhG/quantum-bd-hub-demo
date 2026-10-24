"""Today: who to contact today — overdue and due tasks as cards, plus a side panel with the week's progress,
hand-offs, the email batch and Ghosted decisions."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Activity, Lead, User
from app.services.activity_log import LogError, NextStep, finish_task, ghost_frame, loggable_types, task_frame
from app.services.cadence import IST, keep_open, mark_ghosted, reschedule_activity, run_daily_checks, skip_activity
from app.services.notifications import HANDOFF_MARK
from app.services.procurement import expire_quotes
from app.services.partners import refresh_registrations
from app.services.postsale import sync_installations
from app.services.report_metrics import ensure_monday_task, get_note, monday_of, my_week, save_note
from app.services.signals import hot_score, recompute_scores
from app.services.tasks import (
    TaskError,
    bulk_complete,
    bulk_reschedule,
    email_batch_frame,
    mark_batch_sent,
    mark_task_bounced,
    week_summary,
)
from app.ui.common import require_user
from app.ui.components import avatar, card, channel_tile, chip, chips, day, esc, html_block, progress
from app.ui.layout import load_shell_counts
from app.ui.task_dialog import hand_off_dialog

user = require_user()
FLASH = "edit_flash"
PAGE_SIZE = 25


def _daily_checks() -> None:
    """Once per session per day: finish waiting cadences and run automatic starts."""
    stamp = datetime.now(IST).date().isoformat()
    if st.session_state.get("daily_checks") == stamp:
        return
    with session_scope() as session:
        check = run_daily_checks(session)
        expired = expire_quotes(session)  # sent quotes past their validity
        recompute_scores(session)         # signals fade day by day; replies, demos and quotes change the score
        ensure_monday_task(session)       # admins: prepare last week's report and set this week's targets
        refresh_registrations(session)    # partner registrations: won/lost with their lead, or past the lock
        sync_installations(session)       # every won deal gets a client record
    st.session_state["daily_checks"] = stamp
    if expired:
        st.toast(f"{expired} quote(s) passed their validity and are now marked expired.", icon=":material/timer_off:")
    if check.auto_started.started:
        st.toast(check.auto_started.summary(), icon=":material/rocket_launch:")


@st.cache_data(ttl=30, show_spinner=False)
def _load(owner_id: int | None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, int]]:
    with session_scope() as session:
        return (task_frame(session, owner_id=owner_id), ghost_frame(session, owner_id=owner_id),
                email_batch_frame(session, owner_id), week_summary(session, owner_id))


@st.cache_data(ttl=60, show_spinner=False)
def _my_targets(user_id: int) -> pd.DataFrame:
    with session_scope() as session:
        return my_week(session, user_id)


@st.cache_data(ttl=300, show_spinner=False)
def _team_and_types() -> tuple[list[tuple[int, str]], list[str], int]:
    with session_scope() as session:
        team = [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]
        return team, loggable_types(session), hot_score(session)


def _done(messages: list[str], text: str) -> None:
    st.session_state[FLASH] = ("success", " ".join([text, *messages]), [])
    st.cache_data.clear()
    st.rerun()


# ---------------------------------------------------------------- dialogs


@st.dialog("Mark as done", width="large")
def done_dialog(task: dict) -> None:
    team, types, _ = _team_and_types()
    st.markdown(f"**{task['Task']}** — {task['Institution']}")
    bounced = False
    if task["channel"] == "email":
        bounced = st.checkbox("The email bounced (the address doesn't work)",
                              help="Marks the address as bounced and unverified. If the institution has no other "
                                   "verified contact, its cadence stops.")
    if bounced:
        if st.button("Save as bounced", type="primary", icon=":material/mail_lock:"):
            try:
                with session_scope() as session:
                    messages = mark_task_bounced(session, int(task["activity_id"]), session.get(User, user.id))
            except (TaskError, PermissionError) as error:
                st.error(str(error))
                return
            _done(messages, "Bounce recorded.")
        return
    outcome = st.text_input("Outcome in a line", placeholder="e.g. Sent; or spoke to HoD, wants pricing")
    notes = st.text_area("Notes (optional)", height=80)
    replied = st.checkbox("They replied / the conversation has started",
                          help="Stops the cadence and moves the lead to Replied to Queries.")
    plan = st.toggle("Plan a next step", value=replied)
    next_step = None
    if plan:
        c1, c2, c3 = st.columns(3)
        kind = c1.selectbox("Next step", types, index=types.index("Follow-up task") if "Follow-up task" in types else 0)
        day = c2.date_input("On", value=datetime.now(IST).date() + timedelta(days=2), format="DD/MM/YYYY")
        ids = [uid for uid, _ in team]
        owner = c3.selectbox("Who", ids, format_func=dict(team).get, index=ids.index(user.id) if user.id in ids else 0)
        title = st.text_input("Next step title (optional)", placeholder="e.g. Share brochure and demo slots")
        next_step = NextStep(kind, day, owner_id=owner, title=title or None)
    if st.button("Save", type="primary", icon=":material/check:"):
        try:
            with session_scope() as session:
                messages = finish_task(session, int(task["activity_id"]), session.get(User, user.id),
                                       outcome=outcome or None, notes=notes or None, they_replied=replied,
                                       next_step=next_step)
        except (LogError, PermissionError) as error:
            st.error(str(error))
            return
        _done(messages, "Done.")


@st.dialog("Reschedule")
def reschedule_dialog(task: dict) -> None:
    st.markdown(f"**{task['Task']}** — {task['Institution']}")
    day = st.date_input("New date", value=datetime.now(IST).date() + timedelta(days=1), format="DD/MM/YYYY")
    if st.button("Save", type="primary"):
        with session_scope() as session:
            reschedule_activity(session, session.get(Activity, int(task["activity_id"])), day)
        _done([], f"Moved to {day.strftime('%a %d %b')}.")


@st.dialog("Skip this task")
def skip_dialog(task: dict) -> None:
    st.markdown(f"**{task['Task']}** — {task['Institution']}")
    reason = st.text_input("Why? (optional)", placeholder="e.g. no phone number, out of office")
    st.caption("A skipped cadence step counts as handled, so the cadence moves on to the next step.")
    if st.button("Skip", type="primary"):
        with session_scope() as session:
            messages = skip_activity(session, session.get(Activity, int(task["activity_id"])),
                                     session.get(User, user.id), reason or None)
        _done(messages, "Skipped.")


# ---------------------------------------------------------------- pieces


def _section_title(title: str, count: int, tone: str) -> None:
    # Counts sit in a chip (body font): the heading font draws old-style numerals that read like letters.
    html_block(f'<div style="display:flex;align-items:center;gap:10px;margin-top:0.4rem">'
               f'<h2 class="qa-display" style="margin:0;padding:0;font-size:1.35rem">{esc(title)}</h2>'
               f'{chip(str(count), tone, strong=True)}</div>')


def _due_chip(task: dict) -> tuple[str, str]:
    if task["Bucket"] == "Overdue":
        days = (datetime.now(IST).date() - task["Due by"]).days
        return f"Overdue · {days} day{'s' if days != 1 else ''}", "orange"
    today = datetime.now(IST).date()
    if task["Due by"] > today:
        return f"Due by {task['Due by'].strftime('%a %d %b')}", "blue"
    if task["Due"] < today:
        return "Last day today", "orange"  # inside its grace days, but today is the final one
    return ("Today" if task["Time"] in ("10:00", "00:00") else f"Today · {task['Time']}"), "blue"


def _handed_from(task: dict) -> str:
    if task["From"]:
        return task["From"]
    if HANDOFF_MARK in (task["Note"] or ""):
        return task["Note"].split(HANDOFF_MARK, 1)[1].split(" to ", 1)[0].strip()
    return ""


def task_card(task: dict, show_owner: bool) -> None:
    can = user.can("member")
    late = task["Bucket"] == "Overdue"
    key = task["activity_id"]
    with card(str(key), "late" if late else "plain"):
        # Content first, actions underneath: shorter cards, and the same layout on a phone.
        title = esc(task["Task"])
        if task["Institution"]:
            title += f'<span class="qa-sep">·</span><span style="font-weight:500">{esc(task["Institution"])}</span>'
        person = " — ".join(x for x in (task["Contact"], task["Designation"]) if x)
        reach = []
        if task["Email"]:
            reach.append(f'<a href="mailto:{esc(task["Email"])}" style="color:inherit">{esc(task["Email"])}</a>')
        if task["Phone"]:
            reach.append(f'<a href="tel:{esc(task["Phone"].replace(" ", ""))}" style="color:inherit">'
                         f'{esc(task["Phone"])}</a>')
        due_text, due_tone = _due_chip(task)
        handed = _handed_from(task)
        tags = [task["Track"], task["Step"] or (task["Stage"] if task["Track"] else ""), (due_text, due_tone)]
        if task["lead_id"] and task["Score"] >= HOT:
            tags.insert(0, (f"Hot · score {task['Score']}", "yellow"))
        if show_owner:
            tags.append((f"For {task['Owner']}", "neutral"))
        if handed:
            tags.append((f"From {handed}", "purple"))
        note = task["Note"] if task["Note"] and not task["Cadence"] else ""
        html_block(
            f'<div class="qa-row">{channel_tile(task["channel"])}<div class="qa-row-main">'
            f'<div class="qa-row-title">{title}</div>'
            + (f'<div class="qa-muted">{esc(person)}</div>' if person else "")
            + (f'<div class="qa-small qa-muted">{" · ".join(reach)}</div>' if reach else "")
            + f'<div style="margin-top:4px">{chips(tags)}</div>'
            + (f'<div class="qa-small qa-muted" style="margin-top:4px">Note: {esc(note)}</div>' if note else "")
            + "</div></div>")
        with st.container(horizontal=True, gap="small", key=f"qa-actions-{key}"):
            if st.button("Done", icon=":material/check:", type="primary" if late else "secondary", key=f"done_{key}",
                         disabled=not can):
                done_dialog(task)
            if st.button("Reschedule", key=f"move_{key}", disabled=not can):
                reschedule_dialog(task)
            with st.popover("More", icon=":material/more_horiz:", disabled=not can):
                if st.button("Skip", icon=":material/skip_next:", key=f"skip_{key}", width="stretch"):
                    skip_dialog(task)
                if st.button("Hand off", icon=":material/forward:", key=f"hand_{key}", width="stretch"):
                    hand_off_dialog(user, task)
                if task["lead_id"] and not pd.isna(task["lead_id"]):
                    st.page_link("views/leads.py", label="Open lead", icon=":material/open_in_new:",
                                 query_params={"lead": int(task["lead_id"])}, width="stretch")
            if str(task["Task"]).startswith("Prepare last week's report"):
                st.page_link("views/report.py", label="Open weekly report", icon=":material/summarize:")


def task_section(title: str, frame: pd.DataFrame, key: str, show_owner: bool, tone: str) -> None:
    if frame.empty:
        return
    _section_title(title, len(frame), tone)
    show_all = st.session_state.get(f"all_{key}", False)
    for task in frame.head(None if show_all else PAGE_SIZE).to_dict("records"):
        task_card(task, show_owner)
    if len(frame) > PAGE_SIZE and not show_all:
        if st.button(f"Show all {len(frame)}", key=f"more_{key}"):
            st.session_state[f"all_{key}"] = True
            st.rerun()


def list_view(frame: pd.DataFrame) -> None:
    """Tick several tasks and act on them together."""
    columns = ["Bucket", "Due", "Type", "Task", "Institution", "Contact", "Owner"]
    event = st.dataframe(frame[columns], hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="multi-row", key="today_list",
                         height=min(520, 35 * (len(frame) + 1) + 3),
                         column_config={"Due": st.column_config.DateColumn(format="ddd DD MMM"),
                                        "Bucket": st.column_config.TextColumn("When")})
    picked = event.selection.rows if event and event.selection else []
    ids = frame.iloc[picked]["activity_id"].astype(int).tolist()
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        can = user.can("member") and bool(ids)
        if st.button(f"Mark done ({len(ids)})" if ids else "Mark done", icon=":material/done_all:", type="primary",
                     disabled=not can, key="list_done"):
            with session_scope() as session:
                messages = bulk_complete(session, ids, session.get(User, user.id))
            _done(messages[1:], messages[0])
        with st.popover("Move to a date", icon=":material/event:", disabled=not can):
            day = st.date_input("New date", value=datetime.now(IST).date() + timedelta(days=1), format="DD/MM/YYYY",
                                key="list_day")
            if st.button("Move", type="primary", key="list_move"):
                with session_scope() as session:
                    moved = bulk_reschedule(session, ids, day, session.get(User, user.id))
                _done([], f"{moved} task(s) moved to {day.strftime('%a %d %b')}.")
        st.caption("Tick tasks at the left edge to act on several at once.")


def week_panel(week: dict[str, int], whose: str) -> None:
    with card("week"):
        st.markdown("**Your week**" if whose == "Mine" else f"**{whose} — this week**")
        done, planned = week["done"], max(week["planned"], 1)
        html_block(f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">'
                   f'<span class="qa-muted">Tasks done</span><span><b style="font-size:1.25rem">{done}</b>'
                   f'<span class="qa-muted"> of {week["planned"]}</span></span></div>'
                   + progress(done / planned, f"{done} of {week['planned']} tasks done this week"))
        c1, c2 = st.columns(2)
        c1.metric("Replies", week["replies"], border=True)
        c2.metric("Demos", week["demos"], border=True, help="Demos booked this week")
        if whose == "Mine":
            targets = _my_targets(user.id)
            if not targets.empty:
                st.markdown("**Targets this week**")
                bars = []
                for row in targets.to_dict("records"):
                    done_n, target = int(row["Actual"]), int(row["Target"])
                    bars.append(f'<div style="display:flex;justify-content:space-between;margin-top:6px">'
                                f'<span class="qa-small">{esc(row["Metric"])}</span><span class="qa-small">'
                                f'<b>{done_n}</b><span class="qa-muted"> of {target}</span></span></div>'
                                + progress(min(done_n / target, 1.0) if target else 1.0,
                                           f"{row['Metric']}: {done_n} of {target}"))
                html_block("".join(bars))


def notes_panel() -> None:
    """Each person's note for this week's report (highlights, blockers, asks)."""
    week = monday_of(datetime.now(IST).date())
    with card("report-note"):
        st.markdown("**Notes for this week's report**")
        with session_scope() as session:
            current = get_note(session, user.id, week)
        with st.form("report_note", border=False):
            text = st.text_area("Your note", value=current, height=90, label_visibility="collapsed",
                                placeholder="Highlights, blockers, anything leadership should know")
            if st.form_submit_button("Save note", disabled=not user.can("member")):
                with session_scope() as session:
                    save_note(session, week, text, session.get(User, user.id))
                st.toast("Note saved — it goes into the weekly report.", icon=":material/check:")


def handoff_panel(tasks: pd.DataFrame) -> None:
    passed = [t for t in tasks.to_dict("records") if _handed_from(t)]
    if not passed:
        return
    with card("handoffs", "info"):
        st.markdown(":material/forward: **Handed to you**")
        for task in passed[:4]:
            who = _handed_from(task)
            note = task["Note"].split(":", 1)[1].strip() if HANDOFF_MARK in task["Note"] and ":" in task["Note"] else ""
            html_block(f'<div class="qa-row" style="gap:10px;margin:4px 0">{avatar(who, 30)}<div class="qa-row-main">'
                       f'<div><b>{esc(who)}</b> <span class="qa-muted">· due {day(task["Due"])}</span></div>'
                       f'<div>{esc(task["Task"])}' + (f' — {esc(task["Institution"])}' if task["Institution"] else "")
                       + "</div>" + (f'<div class="qa-small qa-muted">“{esc(note)}”</div>' if note else "")
                       + "</div></div>")


def email_batch_panel(batch: pd.DataFrame) -> None:
    """The email owner's daily routine: copy today's cadence emails into the mail-merge sheet, send, mark all sent."""
    usable = batch[batch["Email"] != ""]
    missing = batch[batch["Email"] == ""]
    with card("batch"):
        html_block(f'<div style="display:flex;justify-content:space-between;align-items:center">'
                   f'<b>Email batch</b>{chip(f"{len(usable)} ready", "blue", strong=True)}</div>')
        by_track = usable.groupby("Track").size().sort_values(ascending=False) if not usable.empty else pd.Series()
        split = ", ".join(f"{track} {n}" for track, n in by_track.items())
        st.caption("Cadence emails due today and overdue, for your mail-merge sheet" + (f" — {split}." if split
                                                                                         else "."))
        stamp = datetime.now(IST).strftime("%Y-%m-%d")
        with st.container(horizontal=True, gap="small"):
            with st.popover("Copy for Sheets", icon=":material/content_copy:", disabled=usable.empty):
                st.caption("Use the copy icon at the top-right of the box, then paste into cell A1 of your sheet.")
                st.code(usable.to_csv(index=False, sep="\t"), language=None)
            st.download_button("CSV", data=usable.to_csv(index=False).encode(), mime="text/csv",
                               file_name=f"email-batch-{stamp}.csv", icon=":material/download:",
                               disabled=usable.empty)
            with st.popover(f"Mark {len(usable)} as sent", type="primary",
                            disabled=usable.empty or not user.can("member")):
                st.caption("Ticks off exactly the emails in this batch; each cadence moves to its next step.")
                if st.button("Yes, all sent", type="primary", key="batch_sent"):
                    with session_scope() as session:
                        messages = mark_batch_sent(session, usable["Task ID"].astype(int).tolist(),
                                                   session.get(User, user.id))
                    _done(messages[1:], messages[0])
        with st.expander("See the emails"):
            st.dataframe(usable.drop(columns=["Task ID", "Lead ID"]), hide_index=True, width="stretch",
                         column_config={"Due": st.column_config.DateColumn(format="DD MMM")})
            if not missing.empty:
                st.caption(f"{len(missing)} email task(s) have no usable address (missing, bounced or "
                           "do-not-contact):")
                st.dataframe(missing[["Name", "Institution", "Track", "Step"]], hide_index=True, width="stretch")


def ghost_panel(ghosts: pd.DataFrame) -> None:
    with card("ghosts"):
        st.markdown("**Cadence finished, no reply**")
        st.caption("Every follow-up was done. Mark Ghosted (the date is recorded) or keep it open.")
        for row in ghosts.to_dict("records"):
            with st.container(gap=None):
                st.page_link("views/leads.py", label=f"**{row['Institution']}**", query_params={"lead": row["lead_id"]})
                st.caption(f"{row['Track']} · no reply by {day(pd.Timestamp(row['No reply since']))}")
                with st.container(horizontal=True, gap="small"):
                    ghost = st.button("Mark Ghosted", key=f"ghost_{row['lead_id']}", disabled=not user.can("member"))
                    keep = st.button("Keep open", key=f"keep_{row['lead_id']}", type="tertiary",
                                     disabled=not user.can("member"))
            if ghost or keep:
                with session_scope() as session:
                    lead, actor = session.get(Lead, int(row["lead_id"])), session.get(User, user.id)
                    (mark_ghosted if ghost else keep_open)(session, lead, actor)
                _done([], f"{row['Institution']}: " + ("marked Ghosted." if ghost else "kept open."))


# ---------------------------------------------------------------- page

_daily_checks()
now = datetime.now(IST)
team, _, HOT = _team_and_types()
greeting = "Good morning" if now.hour < 12 else ("Good afternoon" if now.hour < 17 else "Good evening")

head, tools = st.columns([3, 2], vertical_alignment="bottom")
with tools:
    choices = ["Mine", "Everyone"] + ([name for _, name in team if name != user.name] if user.can("admin") else [])
    whose = st.radio("Whose tasks", choices, horizontal=True, key="today_whose", label_visibility="collapsed")
    with st.container(horizontal=True, gap="small", horizontal_alignment="left"):
        with st.popover("Filter", icon=":material/filter_list:"):
            filter_slot = st.container()
        as_list = st.toggle("List view", key="today_list_view", help="A table where you can tick several tasks at once.")
owner_id = {"Mine": user.id, "Everyone": None} | {name: uid for uid, name in team}
tasks, ghosts, batch, week = _load(owner_id[whose])
show_owner = whose == "Everyone"

with filter_slot:
    kinds = st.multiselect("Type", sorted(tasks["Type"].unique()) if not tasks.empty else [], placeholder="All types")
    tracks = st.multiselect("Track", sorted(t for t in tasks["Track"].unique() if t) if not tasks.empty else [],
                            placeholder="All tracks")
if kinds:
    tasks = tasks[tasks["Type"].isin(kinds)]
if tracks:
    tasks = tasks[tasks["Track"].isin(tracks)]

# Within each section, hotter leads first (lead score), then the oldest due date.
overdue = tasks[tasks["Bucket"] == "Overdue"].sort_values(["Score", "Due"], ascending=[False, True], kind="stable")
due = tasks[tasks["Bucket"] == "Due"].sort_values(["Score", "Due"], ascending=[False, True], kind="stable")
upcoming = tasks[tasks["Bucket"] == "Coming up"]

with head:
    st.caption(now.strftime("%A, %d %B %Y"))
    st.title(f"{greeting}, {user.first_name}" if whose == "Mine" else f"Tasks — {whose}", anchor=False)
    total = len(overdue) + len(due)
    summary = f"{total} task{'s' if total != 1 else ''} today" if total else "Nothing due today"
    if len(overdue):
        summary += f" — {len(overdue)} overdue"
    late_queries = [a for a in load_shell_counts(user.id, user.name).alerts if a.kind == "query"]
    if late_queries and whose == "Mine":
        summary += f". {len(late_queries)} quer{'y has' if len(late_queries) == 1 else 'ies have'} passed the " \
                   "1-day reply target"
    st.markdown(summary + ".")

main, side = st.columns([2.25, 1], gap="large")
with main:
    if tasks.empty and ghosts.empty:
        with card("empty"):
            st.markdown("**Nothing due.** :material/celebration:")
            st.caption("Start cadences from Leads (tick leads → Start cadence), or use **New** at the top to add a "
                       "task or log a call.")
    if as_list:
        now_and_late = pd.concat([overdue, due])
        _section_title("Overdue and due today", len(now_and_late), "blue")
        if now_and_late.empty:
            st.caption("Nothing due.")
        else:
            list_view(now_and_late)
    else:
        task_section("Overdue", overdue, "overdue", show_owner, "orange")
        task_section("Due today", due, "due", show_owner, "blue")
        if overdue.empty and due.empty and not tasks.empty:
            st.caption("Nothing due today — see what's coming up below.")
    with st.expander(f"Coming up — next 7 days · {len(upcoming)}", icon=":material/date_range:"):
        if upcoming.empty:
            st.caption("Nothing planned for the next 7 days.")
        else:
            st.dataframe(upcoming[["Due", "Time", "Type", "Task", "Institution", "Contact", "Owner"]],
                         hide_index=True, width="stretch",
                         column_config={"Due": st.column_config.DateColumn(format="ddd DD MMM")})

with side:
    week_panel(week, whose)
    handoff_panel(tasks)
    if not batch.empty:
        email_batch_panel(batch)
    if not ghosts.empty:
        ghost_panel(ghosts)
    if whose == "Mine":
        notes_panel()
