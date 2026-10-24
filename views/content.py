"""Content calendar for LinkedIn and Instagram: anyone adds ideas and drafts, an admin approves, the poster posts
and marks it posted, and a week later the likes, comments and reposts go in — so we learn which themes work."""

from __future__ import annotations

from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import select
from streamlit_calendar import calendar

from app.db import session_scope
from app.services.cadence import today_ist
from app.models import ContentPost, User
from app.models.content import POST_PLATFORMS
from app.services.content import (
    METRICS,
    PLATFORM_LABELS,
    STATUS_LABELS,
    ContentError,
    PostData,
    approve_post,
    calendar_events,
    create_post,
    drop_post,
    mark_posted,
    posts_frame,
    restore_post,
    save_metrics,
    send_back,
    send_for_review,
    theme_performance,
    update_post,
    warnings_for,
)
from app.services.lead_queries import frame_to_xlsx
from app.services.library import shareable_documents
from app.services.procurement import options
from app.ui.common import require_user
from app.ui.components import card, chip, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
FLASH = "edit_flash"
_TONE = {"idea": "neutral", "draft": "purple", "review": "orange", "approved": "blue", "posted": "green",
         "dropped": "red"}


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


def _me(session) -> User:
    return session.get(User, user.id)


@st.cache_data(ttl=300, show_spinner=False)
def _choices() -> dict:
    with session_scope() as session:
        return {
            "themes": options(session, "content_theme") or ["Product", "Event", "Other"],
            "team": [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True))
                                                             .order_by(User.id))],
            "documents": shareable_documents(session),
        }


@st.cache_data(ttl=60, show_spinner=False)
def _posts() -> pd.DataFrame:
    with session_scope() as session:
        return posts_frame(session)


def _open(post_id: int) -> None:
    st.query_params["post"] = str(post_id)
    st.rerun()


# ---------------------------------------------------------------- new idea


@st.dialog("New post idea", width="large")
def new_dialog() -> None:
    c = _choices()
    title = st.text_input("Working title", placeholder="e.g. Students build an entanglement demo at the hackathon")
    a, b, d = st.columns(3)
    platform = a.selectbox("Platform", POST_PLATFORMS, format_func=PLATFORM_LABELS.get)
    theme = b.selectbox("Theme", c["themes"], index=None, placeholder="Pick one")
    planned = d.date_input("Planned for (optional)", value=None, format="DD/MM/YYYY")
    body = st.text_area("Text (optional now — an idea can be just the title)", height=140)
    if st.button("Save", type="primary", icon=":material/save:", disabled=not user.can("member")):
        try:
            with session_scope() as session:
                post = create_post(session, PostData(title=title, platform=platform, theme=theme,
                                                     planned_on=planned, body=body), _me(session))
                new_id = post.id
        except (ContentError, PermissionError) as error:
            st.error(str(error))
            return
        st.query_params["post"] = str(new_id)
        _done("Saved — finish the draft and send it for approval when ready.")


# ---------------------------------------------------------------- one post


def _load(post_id: int) -> dict | None:
    with session_scope() as session:
        post = session.get(ContentPost, post_id)
        if post is None:
            return None
        names = dict(session.execute(select(User.id, User.name)).all())
        data = {k: getattr(post, k) for k in (
            "id", "title", "platform", "theme", "campaign", "status", "planned_on", "body", "hashtags",
            "asset_links", "document_ids", "author_user_id", "poster_user_id", "approved_on", "review_comment",
            "posted_on", "post_url", "likes", "comments", "reposts", "impressions", "metrics_on", "source_kind")}
        data["author"] = names.get(post.author_user_id, "")
        data["poster"] = names.get(post.poster_user_id, "")
        data["approver"] = names.get(post.approved_by_user_id, "")
        data["warnings"] = warnings_for(session, post)
        return data


def _read_only(p: dict) -> None:
    facts = [("Planned", p["planned_on"].strftime("%d %b %Y") if p["planned_on"] else "—"),
             ("Posted by", p["poster"] or "—"), ("Campaign", p["campaign"] or "—")]
    html_block('<div class="qa-small">' + " · ".join(f'<span class="qa-muted">{esc(k)}:</span> {esc(v)}'
                                                     for k, v in facts) + "</div>")
    st.markdown("**Post text**")
    st.code((p["body"] or "") + (f"\n\n{p['hashtags']}" if p["hashtags"] else ""), language=None, wrap_lines=True)
    if p["asset_links"]:
        st.caption("Links: " + " · ".join(p["asset_links"].splitlines()))


def _editor(p: dict) -> None:
    c = _choices()
    locked = p["status"] in ("posted", "dropped") or not user.can("member")
    if locked:
        _read_only(p)
        return
    with st.form(f"post_form_{p['id']}", border=False):
        title = st.text_input("Working title (team only)", value=p["title"])
        a, b, d, e = st.columns([1.3, 1.1, 1, 1])
        platform = a.selectbox("Platform", POST_PLATFORMS, format_func=PLATFORM_LABELS.get,
                               index=POST_PLATFORMS.index(p["platform"]))
        themes = c["themes"]
        theme = b.selectbox("Theme", themes, index=themes.index(p["theme"]) if p["theme"] in themes else None,
                            placeholder="Pick one")
        planned = d.date_input("Planned for", value=p["planned_on"], format="DD/MM/YYYY")
        ids = [uid for uid, _ in c["team"]]
        poster = e.selectbox("Who posts it", ids, format_func=dict(c["team"]).get,
                             index=ids.index(p["poster_user_id"]) if p["poster_user_id"] in ids else 0)
        campaign = st.text_input("Campaign (optional)", value=p["campaign"] or "",
                                 placeholder="e.g. NQM labs push, Quantum Hackathon 2026")
        body = st.text_area("Post text", value=p["body"] or "", height=260,
                            help="LinkedIn allows 3,000 characters; Instagram captions 2,200. The first two lines "
                                 "are what people see before 'see more'.")
        hashtags = st.text_input("Hashtags", value=p["hashtags"] or "",
                                 placeholder="QuantumIndia NQM QuantumEducation (the # is added for you)")
        a, b = st.columns(2)
        docs = [did for did, _ in c["documents"]]
        if docs:
            picked_docs = a.multiselect("Images / videos from the Library", docs, format_func=dict(c["documents"]).get,
                                        default=[d for d in (p["document_ids"] or []) if d in docs])
        else:
            picked_docs = []
            a.caption("Approved images and videos in the Library can be attached here.")
        links = b.text_area("Other links (one per line)", value=p["asset_links"] or "", height=68,
                            placeholder="Canva design, Drive folder, source article…")
        saved = st.form_submit_button("Save", type="primary", icon=":material/save:")
    st.caption(f"{len(body or ''):,} characters" + (" · Instagram limit 2,200" if platform in ("instagram", "both")
                                                    else " · LinkedIn limit 3,000"))
    if saved:
        try:
            with session_scope() as session:
                message = update_post(session, p["id"], PostData(
                    title=title, platform=platform, theme=theme, campaign=campaign, planned_on=planned, body=body,
                    hashtags=hashtags, asset_links=links, document_ids=picked_docs, poster_user_id=poster),
                    _me(session))
        except (ContentError, PermissionError) as error:
            st.error(str(error))
        else:
            _done(message or "Saved.", "warning" if message else "success")


def _actions(p: dict) -> None:
    pid, status = p["id"], p["status"]
    if status in ("idea", "draft") and user.can("member"):
        if p["review_comment"]:
            st.warning(f"**Sent back by the admin:** {p['review_comment']}", icon=":material/rate_review:")
        if st.button("Send for approval", type="primary", icon=":material/send:", key=f"review_{pid}",
                     disabled=not p["body"], help=None if p["body"] else "Write the post text first."):
            try:
                with session_scope() as session:
                    notes = send_for_review(session, pid, _me(session))
            except (ContentError, PermissionError) as error:
                st.error(str(error))
            else:
                _done("Sent for approval." + (f" The admin will see {len(notes)} thing(s) to check." if notes
                                              else ""))
    if status in ("review", "draft") and user.can("admin"):
        _approve_box(p)
    if status == "review" and not user.can("admin"):
        st.info("Waiting for the admin's approval. Editing the text is still possible until then.",
                icon=":material/hourglass_top:")
    if status == "approved":
        st.success(f"Approved by {p['approver']} on {p['approved_on']:%d %b}. {p['poster']} posts it on "
                   f"{p['planned_on']:%d %b %Y}.", icon=":material/verified:")
        st.markdown("**Copy the text** (the copy icon is at the top right of the box)")
        st.code((p["body"] or "") + (f"\n\n{p['hashtags']}" if p["hashtags"] else ""), language=None,
                wrap_lines=True)
        if user.can("member"):
            with st.form(f"posted_{pid}", border=True):
                st.markdown("**After posting**")
                a, b = st.columns([1.8, 1.2])
                url = a.text_input("Link to the post", placeholder="https://www.linkedin.com/posts/…")
                when = b.date_input("Posted on", value=today_ist(), format="DD/MM/YYYY")
                if st.form_submit_button("Mark as posted", type="primary", icon=":material/task_alt:"):
                    try:
                        with session_scope() as session:
                            message = mark_posted(session, pid, _me(session), url=url, posted_on=when)
                    except (ContentError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        _done(message)
    if status == "posted":
        _metrics_box(p)
    if status != "posted" and user.can("member"):
        with st.container(horizontal=True, gap="small"):
            if status == "dropped":
                if st.button("Restore", icon=":material/restore:", key=f"restore_{pid}"):
                    with session_scope() as session:
                        restore_post(session, pid, _me(session))
                    _done("Restored.")
            elif user.can("admin") or p["author_user_id"] == user.id:
                with st.popover("Drop", icon=":material/delete:", type="tertiary"):
                    st.caption("Keeps it in the list as Dropped (you can restore it).")
                    if st.button("Drop this post", key=f"drop_{pid}"):
                        with session_scope() as session:
                            drop_post(session, pid, _me(session))
                        _done("Dropped.")


def _approve_box(p: dict) -> None:
    pid = p["id"]
    c = _choices()
    with st.container(border=True):
        st.markdown("**Approve**")
        if p["warnings"]:
            st.warning("**Check before approving:**\n\n" + "\n".join(f"- {w}" for w in p["warnings"]),
                       icon=":material/fact_check:")
        a, b = st.columns(2)
        day = a.date_input("Post on", value=max(p["planned_on"] or today_ist(), today_ist()), format="DD/MM/YYYY",
                           key=f"ap_day_{pid}")
        ids = [uid for uid, _ in c["team"]]
        poster = b.selectbox("Posted by", ids, format_func=dict(c["team"]).get, key=f"ap_poster_{pid}",
                             index=ids.index(p["poster_user_id"]) if p["poster_user_id"] in ids else 0)
        comment = st.text_input("Comment (needed to send back)", key=f"ap_comment_{pid}")
        with st.container(horizontal=True, gap="small"):
            ok = st.button("Approve", type="primary", icon=":material/verified:", key=f"ap_ok_{pid}",
                           disabled=not p["body"])
            back = st.button("Send back", icon=":material/undo:", key=f"ap_back_{pid}")
        if ok or back:
            try:
                with session_scope() as session:
                    if ok:
                        message = approve_post(session, pid, _me(session), planned_on=day, poster_user_id=poster)
                    else:
                        send_back(session, pid, comment, _me(session))
                        message = "Sent back to the author with your comment."
            except (ContentError, PermissionError) as error:
                st.error(str(error))
            else:
                _done(message)


def _metrics_box(p: dict) -> None:
    pid = p["id"]
    due = p["posted_on"] + timedelta(days=7) if p["posted_on"] else None
    caption = (f"Posted {p['posted_on']:%d %b %Y} by {p['poster']}." if p["posted_on"] else "Posted.")
    if p["post_url"]:
        st.link_button("Open the post", p["post_url"], icon=":material/open_in_new:")
    st.caption(caption + (f" Numbers last added {p['metrics_on']:%d %b}." if p["metrics_on"] else
                          f" Add the numbers on or after {due:%d %b} (a week in)." if due else ""))
    with st.form(f"metrics_{pid}", border=True):
        st.markdown("**Engagement**")
        cols = st.columns(4)
        values = {}
        for col, name in zip(cols, METRICS):
            values[name] = col.number_input(name.title(), min_value=0, step=1, value=p[name],
                                            help="From the post's analytics" if name == "impressions" else None)
        if st.form_submit_button("Save numbers", type="primary", icon=":material/save:",
                                 disabled=not user.can("member")):
            try:
                with session_scope() as session:
                    save_metrics(session, pid, values, _me(session))
            except (ContentError, PermissionError) as error:
                st.error(str(error))
            else:
                _done("Engagement saved.")


def post_page(post_id: int) -> None:
    p = _load(post_id)
    if st.button("All posts", icon=":material/arrow_back:", type="tertiary", key="post_back"):
        del st.query_params["post"]
        st.rerun()
    if p is None:
        st.warning("That post no longer exists.")
        return
    with card("post-head"):
        html_block(f'<h1 class="qa-display" style="margin:0;padding:0;font-size:1.6rem">{esc(p["title"])}</h1>'
                   f'<div style="margin-top:6px">{chips([(STATUS_LABELS[p["status"]], _TONE[p["status"]]), (PLATFORM_LABELS[p["platform"]], "blue"), p["theme"] or "", p["campaign"] or "", ("By " + p["author"]) if p["author"] else ""])}</div>')
    left, right = st.columns([1.5, 1], gap="large")
    with left:
        _editor(p)
    with right:
        _actions(p)
        if p["warnings"] and p["status"] in ("idea", "draft"):
            st.warning("**Before sending for approval:**\n\n" + "\n".join(f"- {w}" for w in p["warnings"]),
                       icon=":material/fact_check:")


# ---------------------------------------------------------------- page tabs


def calendar_tab() -> None:
    today = today_ist()
    start, end = today - timedelta(days=120), today + timedelta(days=240)
    with session_scope() as session:
        events = calendar_events(session, start, end)
    html_block(chips([(label, tone) for key, (label, tone) in {
        k: (STATUS_LABELS[k], _TONE[k]) for k in ("idea", "draft", "review", "approved", "posted")}.items()]))
    state = calendar(events=events, key=f"content_cal_{len(events)}", options={
        "initialView": "dayGridMonth", "firstDay": 1, "height": 640,
        "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,listMonth"},
        "buttonText": {"today": "Today", "month": "Month", "list": "List"}},
        custom_css=".fc { font-family: Inter, system-ui, sans-serif; font-size: 0.85rem; } "
                   ".fc .fc-button-primary { background:#7A4A26; border-color:#7A4A26; } .fc-event { cursor:pointer; }")
    clicked = (state or {}).get("eventClick", {}).get("event") if isinstance(state, dict) else None
    if clicked:
        _open(int(clicked["id"]))
    st.caption("Only posts with a planned date appear here. Click one to open it.")


def list_tab(posts: pd.DataFrame) -> None:
    a, b = st.columns([2, 1])
    statuses = a.multiselect("Status", list(STATUS_LABELS.values()),
                             default=[s for s in STATUS_LABELS.values() if s != "Dropped"], key="post_status")
    themes = b.multiselect("Theme", sorted(t for t in posts["Theme"].unique() if t), placeholder="All themes",
                           key="post_theme")
    view = posts[posts["Status"].isin(statuses)] if statuses else posts
    if themes:
        view = view[view["Theme"].isin(themes)]
    shown = view.drop(columns=["id", "Text"])
    picked = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                          key="posts_table", height=min(460, 35 * (len(shown) + 1) + 3),
                          column_config={"Planned": st.column_config.DateColumn(format="DD MMM YYYY"),
                                         "Posted": st.column_config.DateColumn(format="DD MMM YYYY"),
                                         "Link": st.column_config.LinkColumn(display_text="Open"),
                                         "Engagement rate": st.column_config.NumberColumn(format="%.1f%%")})
    rows = picked.selection.rows if picked and picked.selection else []
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.download_button("Posts (Excel)", data=frame_to_xlsx(view.drop(columns=["id"]), "Posts"),
                           icon=":material/download:", file_name=f"content-{today_ist().isoformat()}.xlsx")
        if rows:
            chosen = view.iloc[rows[0]]
            if st.button(f"Open “{chosen['Title'][:40]}”", type="primary", icon=":material/open_in_new:",
                         key="post_open"):
                _open(int(chosen["id"]))
        else:
            st.caption("Tick a post at the left edge to open it.")


def approval_tab(posts: pd.DataFrame) -> None:
    waiting = posts[posts["Status"] == STATUS_LABELS["review"]]
    if waiting.empty:
        st.caption("Nothing waiting for approval.")
        return
    if not user.can("admin"):
        st.caption("An admin approves these. You can still open and edit them.")
    for row in waiting.to_dict("records"):
        with card(f"wait-{row['id']}"):
            info, go = st.columns([4, 1], vertical_alignment="center")
            planned = row["Planned"].strftime("%d %b") if pd.notna(row["Planned"]) else "No date"
            preview = (row["Text"][:220] + "…") if len(row["Text"]) > 220 else row["Text"]
            info.html(f'<b>{esc(row["Title"])}</b><div>{chips([row["Platform"], row["Theme"], planned, "By " + row["Author"]])}'
                      f'</div><div class="qa-small qa-muted" style="margin-top:4px">{esc(preview)}</div>')
            if go.button("Review", type="primary" if user.can("admin") else "secondary", key=f"go_{row['id']}"):
                _open(int(row["id"]))


def numbers_tab(posts: pd.DataFrame) -> None:
    today = today_ist()
    due = posts[(posts["Status"] == "Posted") & posts["Engagements"].isna()]
    due = due[pd.to_datetime(due["Posted"]) <= pd.Timestamp(today - timedelta(days=7))] if not due.empty else due
    if not due.empty:
        st.markdown(f"**Waiting for numbers · {len(due)}**")
        st.caption("Posted a week or more ago with no likes or comments added yet.")
        for row in due.to_dict("records"):
            line, go = st.columns([4, 1], vertical_alignment="center")
            line.markdown(f"{row['Title']} · {row['Platform']} · posted {row['Posted']:%d %b}")
            if go.button("Add numbers", key=f"num_{row['id']}"):
                _open(int(row["id"]))
    period = st.radio("Period", ["Last 12 weeks", "This FY", "All time"], horizontal=True, key="theme_period")
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    start = {"Last 12 weeks": today - timedelta(weeks=12), "This FY": fy_start}.get(period)
    with session_scope() as session:
        themes = theme_performance(session, start, today)
    measured = themes[themes["With numbers"] > 0] if not themes.empty else themes
    if measured.empty:
        st.caption("No engagement numbers yet. A week after a post goes live, Today asks the poster to add them.")
        return
    st.markdown("**Average engagements per post, by theme**")
    st.caption("Engagements = likes + comments + reposts. The rate divides by impressions where those were added.")
    ink = "#F3EADB" if st.context.theme.type == "dark" else "#2B1C10"
    colour = "#D29B65" if st.context.theme.type == "dark" else "#8B5A2B"
    bars = alt.Chart(measured).mark_bar(cornerRadiusEnd=4, color=colour, height={"band": 0.6}).encode(
        y=alt.Y("Theme:N", sort="-x", title=None, axis=alt.Axis(labelColor=ink, labelLimit=200, domain=False,
                                                                ticks=False)),
        x=alt.X("Avg engagements per post:Q", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False,
                                                                        grid=False)),
        tooltip=["Theme", "With numbers", "Avg engagements per post", "Engagement rate %"])
    labels = bars.mark_text(align="left", dx=6, fontWeight=600, color=ink).encode(
        text=alt.Text("Avg engagements per post:Q", format=".1~f"))
    st.altair_chart((bars + labels).properties(height=34 * len(measured)), width="stretch")
    st.dataframe(themes, hide_index=True, width="stretch",
                 column_config={"Engagement rate %": st.column_config.NumberColumn(format="%.1f%%")})
    st.download_button("Themes (Excel)", data=frame_to_xlsx(themes, "Themes"), icon=":material/download:",
                       file_name=f"content-themes-{today.isoformat()}.xlsx")


# ---------------------------------------------------------------- page

_linked = st.query_params.get("post")
if _linked and str(_linked).isdigit():
    post_page(int(_linked))
    st.stop()

head, tools = st.columns([4, 1.2], vertical_alignment="bottom")
with head:
    page_header("Content", "LinkedIn and Instagram: ideas → drafts → admin approval → posted, and the likes and "
                "comments a week later so we see which themes work.", eyebrow="Marketing")
with tools:
    if st.button("New idea", icon=":material/add:", type="primary", width="stretch", disabled=not user.can("member")):
        new_dialog()

posts = _posts()
if posts.empty:
    empty_state("No posts yet. Add an idea with New idea — or use “Make a post” on a news item or an event.")
    st.stop()

month_start = today_ist().replace(day=1)
posted = posts[posts["Status"] == "Posted"]
m = st.columns(4)
waiting_n = int((posts["Status"] == STATUS_LABELS["review"]).sum())
m[0].metric("Waiting for approval", waiting_n, border=True)
m[1].metric("Approved, not yet posted", int((posts["Status"] == "Approved").sum()), border=True)
m[2].metric("Posted this month", int((pd.to_datetime(posted["Posted"]) >= pd.Timestamp(month_start)).sum()),
            border=True)
measured = posted["Engagements"].dropna()
m[3].metric("Avg engagements per post", f"{measured.mean():.1f}" if not measured.empty else "—", border=True,
            help="Likes + comments + reposts, over posts with numbers added.")

t_cal, t_list, t_wait, t_numbers = st.tabs(["Calendar", "All posts", f"Approval · {waiting_n}" if waiting_n
                                            else "Approval", "What works"])
with t_cal:
    calendar_tab()
with t_list:
    list_tab(posts)
with t_wait:
    approval_tab(posts)
with t_numbers:
    numbers_tab(posts)
html_block(f'<div class="qa-small qa-muted" style="margin-top:8px">{chip("Tip", "blue")} The hub doesn\'t post for '
           "you: copy the approved text into LinkedIn or Instagram (or their scheduler), then mark it posted with "
           "the link.</div>")
