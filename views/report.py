"""Weekly report: a leadership dashboard for any week — outreach, follow-ups by calls and emails, leads by stage,
deals closed, pipeline, team and targets, public/private and urban/rural segments, hot leads and comments — with
Word (on the letterhead) and Excel downloads, weekly targets and saved history."""

from __future__ import annotations

from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from app.db import session_scope
from app.models import User
from app.services.cadence import today_ist
from app.services.money import inr
from app.services.procurement import get_settings
from app.services.quote_docx import template_bytes
from app.services.report_docx import report_docx, report_xlsx
from app.services.report_metrics import (
    TARGET_METRICS,
    ReportError,
    default_week,
    finalise,
    history_frame,
    monday_of,
    reopen,
    report_for,
    save_comments,
    save_targets,
    targets_frame,
    week_label,
)
from app.ui.common import require_user
from app.ui.components import card, chip, esc, html_block
from app.ui.layout import page_header

user = require_user()
FLASH = "edit_flash"
WEEKS_BACK = 26


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=600, show_spinner="Working out the week's numbers…")
def _report(week_iso: str) -> tuple[dict, dict, str]:
    week = pd.Timestamp(week_iso).date()
    with session_scope() as session:
        report, row = report_for(session, week)
        comments = {"highlights": row.highlights if row else None, "risks": row.risks if row else None,
                    "asks": row.asks if row else None}
        return report, comments, row.status if row else "draft"


def _theme() -> tuple[str, str, list[str]]:
    dark = st.context.theme.type == "dark"
    # Ink, grid and the hub's validated categorical palette for the active mode.
    return ("#F3EADB", "#3D3026", ["#D29B65", "#4FB0A8", "#22A35A"]) if dark else \
        ("#2B1C10", "#EDE3D6", ["#8B5A2B", "#2F7F7A", "#16A34A"])


# ---------------------------------------------------------------- charts


def followup_chart(rows: list[dict]) -> None:
    ink, grid, palette = _theme()
    if not rows:
        st.caption("No follow-ups this week or last.")
        return
    frame = pd.DataFrame(rows).melt(id_vars="Channel", var_name="Week", value_name="Count")
    base = alt.Chart(frame).encode(
        y=alt.Y("Channel:N", sort=[r["Channel"] for r in rows], title=None,
                axis=alt.Axis(labelColor=ink, domain=False, ticks=False, labelLimit=160)),
        yOffset=alt.YOffset("Week:N", sort=["This week", "Last week"]),
        x=alt.X("Count:Q", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False, gridColor=grid)),
        color=alt.Color("Week:N", scale=alt.Scale(domain=["This week", "Last week"], range=palette[:2]),
                        legend=alt.Legend(orient="top", title=None, labelColor=ink)),
        tooltip=["Channel:N", "Week:N", alt.Tooltip("Count:Q", format=",")])
    bars = base.mark_bar(cornerRadiusEnd=4, height={"band": 0.8})
    labels = base.mark_text(align="left", dx=4, color=ink, fontSize=11).encode(text=alt.Text("Count:Q", format=","))
    st.altair_chart((bars + labels).properties(height=max(120, 56 * len(rows))), width="stretch")


def stage_chart(rows: list[dict]) -> None:
    ink, grid, palette = _theme()
    frame = pd.DataFrame(rows)
    frame["order"] = range(len(frame))
    frame["Label"] = [f"{n:,}" + (f"  (+{m} this week)" if m else "") for n, m in
                      zip(frame["Leads"], frame["Moved in this week"])]
    bars = alt.Chart(frame).mark_bar(cornerRadiusEnd=4, height={"band": 0.62}, color=palette[0]).encode(
        y=alt.Y("Stage:N", sort=alt.SortField("order"), title=None,
                axis=alt.Axis(labelColor=ink, domain=False, ticks=False, labelLimit=180)),
        x=alt.X("Leads:Q", title=None, axis=alt.Axis(labels=False, ticks=False, domain=False, gridColor=grid)),
        tooltip=["Stage:N", alt.Tooltip("Leads:Q", format=","), alt.Tooltip("Moved in this week:Q", format=",")])
    labels = bars.mark_text(align="left", dx=6, color=ink, fontSize=11).encode(text="Label:N")
    st.altair_chart((bars + labels).properties(height=30 * len(frame)), width="stretch")


def trend_chart(trend: dict) -> None:
    ink, grid, palette = _theme()
    names = ["Outreach", "Follow-ups", "Replies"]
    frame = pd.DataFrame([{"Week": label, "order": i, "Series": name, "Count": trend[name][i]}
                          for name in names for i, label in enumerate(trend["labels"])])
    base = alt.Chart(frame).encode(
        x=alt.X("Week:N", sort=alt.SortField("order"), title=None,
                axis=alt.Axis(labelColor=ink, labelAngle=0, domain=False, ticks=False)),
        y=alt.Y("Count:Q", title=None, axis=alt.Axis(labelColor=ink, gridColor=grid, domain=False, ticks=False,
                                                     tickCount=4)),
        color=alt.Color("Series:N", scale=alt.Scale(domain=names, range=palette),
                        legend=alt.Legend(orient="top", title=None, labelColor=ink)),
        # Replies are dashed: a second cue besides colour for the orange/green pair.
        strokeDash=alt.StrokeDash("Series:N", scale=alt.Scale(domain=names, range=[[1, 0], [1, 0], [5, 3]]),
                                  legend=None),
        tooltip=["Week:N", "Series:N", alt.Tooltip("Count:Q", format=",")])
    chart = base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)
    st.altair_chart(chart.properties(height=230), width="stretch")


def segment_chart(rows: list[dict]) -> None:
    ink, grid, palette = _theme()
    frame = pd.DataFrame(rows)[["Segment", "Contacted (week)", "Replied (week)"]].melt(
        id_vars="Segment", var_name="Measure", value_name="Count")
    domain = ["Contacted (week)", "Replied (week)"]
    base = alt.Chart(frame).encode(
        x=alt.X("Segment:N", sort=[r["Segment"] for r in rows], title=None,
                axis=alt.Axis(labelColor=ink, labelAngle=0, domain=False, ticks=False)),
        xOffset=alt.XOffset("Measure:N", sort=domain),
        y=alt.Y("Count:Q", title=None, axis=alt.Axis(labelColor=ink, gridColor=grid, domain=False, ticks=False,
                                                     tickCount=4)),
        color=alt.Color("Measure:N", scale=alt.Scale(domain=domain, range=[palette[0], palette[2]]),
                        legend=alt.Legend(orient="top", title=None, labelColor=ink)),
        tooltip=["Segment:N", "Measure:N", alt.Tooltip("Count:Q", format=",")])
    st.altair_chart(base.mark_bar(cornerRadiusEnd=4).properties(height=220), width="stretch")


# ---------------------------------------------------------------- the report


def _money_text(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Indian-format rupee text (₹35,50,000) — the table's number formats can't group lakhs and crores."""
    frame = frame.copy()
    for column in columns:
        if column in frame:
            frame[column] = frame[column].map(lambda v: inr(v or 0, decimals=False))
    return frame


def tiles(report: dict) -> None:
    headline = report["headline"]
    for row in (headline[:4], headline[4:8]):
        for col, tile in zip(st.columns(4), row):
            col.metric(tile["label"], f"{tile['now']:,}", delta=tile["now"] - tile["before"], border=True,
                       help=tile["help"], delta_description="vs last week")
    totals = report["totals"]
    rate = totals.get("reply_rate")
    facts = [
        ("Pipeline (excl. GST)", inr(totals["pipeline"], decimals=False),
         f"{totals['open_quotes']} open quotation(s) · weighted {inr(totals['pipeline_weighted'], decimals=False)}"),
        ("Quotations sent", inr(totals["quotes_value"], decimals=False), "value sent this week, excl. GST"),
        ("Deals won", inr(totals["won_value"], decimals=False), "purchase orders dated this week"),
        ("Reply rate", f"{rate:.0%}" if rate is not None else "—",
         f"of {totals['reply_cohort']} organisations first contacted in the last 4 weeks" if rate is not None
         else "no first contacts in the last 4 weeks"),
    ]
    with st.container(border=True, key="qa-card-plain-report-facts"):
        for col, (label, value, note) in zip(st.columns(4), facts):
            col.html(f'<div class="qa-small qa-muted">{esc(label)}</div><div style="font-size:1.35rem;'
                     f'font-weight:600">{esc(value)}</div><div class="qa-small qa-muted">{esc(note)}</div>')


def render_report(report: dict, comments: dict, status: str) -> None:
    tiles(report)
    left, right = st.columns(2, gap="large")
    with left, card("rep-follow"):
        st.markdown("**Follow-ups by channel**")
        st.caption("After the first contact. Calls include WhatsApp calls.")
        followup_chart(report["followups"])
    with right, card("rep-stages"):
        st.markdown("**Leads by stage — end of the week**")
        st.caption("Rebuilt from the stage history, so a past week shows what was true then.")
        stage_chart(report["stages"])
    left, right = st.columns(2, gap="large")
    with left, card("rep-trend"):
        st.markdown("**Last eight weeks**")
        trend_chart(report["trend"])
    with right, card("rep-deals"):
        st.markdown("**Deals closed**")
        if report["won"]:
            st.dataframe(_money_text(pd.DataFrame(report["won"]), ["Value (₹)"]), hide_index=True, width="stretch")
        else:
            st.caption("No purchase orders this week.")
        if report["lost"]:
            st.markdown("Lost")
            st.dataframe(pd.DataFrame(report["lost"]), hide_index=True, width="stretch")
        if report["hot"]:
            st.markdown("**Hot leads**")
            st.dataframe(pd.DataFrame(report["hot"])[["Organisation", "Stage", "Score"]], hide_index=True,
                         width="stretch",
                         column_config={"Score": st.column_config.NumberColumn(format="%d", width="small")})
    with card("rep-team"):
        st.markdown("**Team**")
        st.caption("Outreach = organisations contacted for the first time. Replies are credited to the lead's owner.")
        if report["people"]:
            st.dataframe(pd.DataFrame(report["people"]), hide_index=True, width="stretch")
        if report["targets"]:
            st.markdown("**Targets vs actual**")
            st.dataframe(pd.DataFrame(report["targets"]), hide_index=True, width="stretch",
                         column_config={"Progress": st.column_config.ProgressColumn(format="percent", min_value=0.0,
                                                                                    max_value=1.0)})
        else:
            st.caption("No targets were set for this week (Targets tab).")
    if report["segments"]:
        with card("rep-segments"):
            st.markdown("**Public and private, urban and rural**")
            st.caption("Public/private from the organisation's management; urban/rural from its area. Quotes and "
                       "wins are this financial year so far.")
            segment_chart(report["segments"])
            st.dataframe(_money_text(pd.DataFrame(report["segments"]), ["Won value (FY, ₹)", "Pipeline now (₹)"]),
                         hide_index=True, width="stretch")


def community_box(report: dict) -> None:
    data = report.get("community")  # reports finalised before Week 9 don't have this section
    if not data:
        return
    with card("rep-community"):
        st.markdown("**Events & community**")
        c = st.columns(3)
        c[0].metric("Events this week", len(data["events"]), border=True)
        c[1].metric("New community members", data["new_members"], border=True)
        c[2].metric("Community (mailable)", f"{data['members']:,} ({data['mailable']:,})", border=True,
                    help="Mailable = consent + 18 or older.")
        if data["events"]:
            st.dataframe(pd.DataFrame(data["events"]), hide_index=True, width="stretch")
        if data["top_institutions"]:
            st.caption("Most participants this week: " + ", ".join(
                f"{r['Organisation']} ({r['Participants']})" for r in data["top_institutions"]))


def content_box(report: dict) -> None:
    data = report.get("content")  # reports finalised before 2026-10-21 don't have this section
    if not data:
        return
    with card("rep-content"):
        st.markdown("**LinkedIn & Instagram**")
        c = st.columns(3)
        c[0].metric("Posts this week", data["posted"], border=True,
                    help=f"LinkedIn {data['linkedin']} · Instagram {data['instagram']}")
        c[1].metric("Approved for next week", data["approved_next_week"], border=True)
        c[2].metric("Waiting for approval", data["waiting_approval"], border=True)
        if data["best_post"]:
            best = data["best_post"]
            st.caption(f"Best post so far: **{best['Post']}** ({best['Theme']}) — {best['Engagements']} engagements.")
        if data["themes"]:
            st.markdown("Which themes do best — last 12 weeks")
            st.dataframe(pd.DataFrame(data["themes"]), hide_index=True, width="stretch",
                         column_config={"Engagement rate %": st.column_config.NumberColumn(format="%.1f%%")})
        else:
            st.caption("No engagement numbers yet — they're added a week after each post on the Content page.")


def comments_box(week, report: dict, comments: dict, status: str) -> None:
    with card("rep-comments"):
        st.markdown("**Comments for leadership**")
        if user.can("admin") and status != "final":
            with st.form("rep_comments", border=False):
                highlights = st.text_area("Highlights", value=comments["highlights"] or "", height=100,
                                          placeholder="One per line — e.g. Ghaggar University selected for an NQM lab")
                risks = st.text_area("Risks and blockers", value=comments["risks"] or "", height=90)
                asks = st.text_area("Support needed", value=comments["asks"] or "", height=80)
                if st.form_submit_button("Save comments", type="primary", icon=":material/save:"):
                    with session_scope() as session:
                        save_comments(session, week, highlights=highlights, risks=risks, asks=asks,
                                      user=session.get(User, user.id))
                    _done("Comments saved.")
        else:
            for title, key in (("Highlights", "highlights"), ("Risks and blockers", "risks"),
                               ("Support needed", "asks")):
                st.markdown(f"*{title}*")
                st.markdown("\n".join(f"- {line}" for line in (comments[key] or "").splitlines() if line.strip())
                            or "None added.")
        if report["notes"]:
            st.markdown("**From the team**")
            for note in report["notes"]:
                st.markdown(f"**{note['Person']}:** {note['Note']}")
        else:
            st.caption("Team notes appear here — each person writes theirs on Today (“Notes for this week's "
                       "report”).")


def report_tab(week) -> None:
    report, comments, status = _report(week.isoformat())
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        html_block(chip("Final — numbers frozen" if status == "final" else "Draft — live numbers",
                        "green" if status == "final" else "blue", strong=True))
        st.caption(f"Numbers as of {report['generated_at']} IST.")
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        with session_scope() as session:
            settings = get_settings(session)
        title = settings.get(f"title:{user.email.lower()}")
        prepared = f"{user.name}, {title}" if title else user.name
        stamp = week.isoformat()
        st.download_button("Word report", icon=":material/description:", type="primary",
                           data=report_docx(report, template=template_bytes(settings), comments=comments,
                                            prepared_by=prepared, status=status),
                           file_name=f"weekly-report-{stamp}.docx", on_click="ignore",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        st.download_button("Excel", icon=":material/table_view:", data=report_xlsx(report, comments),
                           file_name=f"weekly-report-{stamp}.xlsx", on_click="ignore",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if user.can("admin"):
            if status == "final":
                if st.button("Reopen", icon=":material/lock_open:", key="rep_reopen",
                             help="Back to live numbers so you can change the comments."):
                    with session_scope() as session:
                        reopen(session, week, session.get(User, user.id))
                    _done("Report reopened.")
            elif st.button("Finalise", icon=":material/lock:", key="rep_final",
                           help="Freezes the numbers, so what leadership saw never changes when activities are "
                                "edited later. You can reopen it."):
                with session_scope() as session:
                    finalise(session, week, session.get(User, user.id))
                _done(f"Report for {week_label(week)} finalised.")
    render_report(report, comments, status)
    community_box(report)
    content_box(report)
    comments_box(week, report, comments, status)


def targets_tab() -> None:
    this_monday = monday_of(today_ist())
    options = [this_monday + timedelta(days=7 * i) for i in (1, 0, -1, -2, -3, -4)]
    week = st.selectbox("Targets for the week of", options, index=1, format_func=week_label, key="tgt_week")
    with session_scope() as session:
        frame, copied = targets_frame(session, week)
    if copied:
        st.info("No targets saved for this week yet — these are last time's. Adjust and save.", icon=":material/info:")
    st.caption("One row per person. Leave a cell blank for no target. Progress shows on each person's Today and in "
               "the report.")
    admin = user.can("admin")
    edited = st.data_editor(
        frame, hide_index=True, width="stretch", key=f"tgt_grid_{week.isoformat()}", placeholder="",
        disabled=not admin or ["user_id", "Person"], column_order=["Person", *TARGET_METRICS.values()],
        column_config={label: st.column_config.NumberColumn(min_value=0, max_value=100000, step=1, format="%d")
                       for label in TARGET_METRICS.values()})
    if admin and st.button("Save targets", type="primary", icon=":material/save:", key="tgt_save"):
        try:
            with session_scope() as session:
                n = save_targets(session, week, edited, session.get(User, user.id))
        except (ReportError, PermissionError) as error:
            st.error(str(error))
        else:
            _done(f"Targets for {week_label(week)} saved ({n} change(s)).")
    if not admin:
        st.caption("Only an admin can set targets.")


def history_tab() -> None:
    with session_scope() as session:
        history = history_frame(session)
    if history.empty:
        st.caption("No reports saved yet. Comments and finalised reports appear here.")
        return
    st.dataframe(history.drop(columns=["week_start"]), hide_index=True, width="stretch")
    st.caption("Pick the week at the top of the page to open, download or reopen a saved report.")


# ---------------------------------------------------------------- page

head, tools = st.columns([3, 1.3], vertical_alignment="bottom")
with head:
    page_header("Weekly report", "Outreach, follow-ups, the pipeline and the team's week — for the Monday review "
                "and senior leadership. Download it as Word or Excel.", eyebrow="Overview")
with tools:
    current = monday_of(today_ist())
    weeks = [current - timedelta(days=7 * i) for i in range(WEEKS_BACK)]
    default = default_week()
    chosen = st.selectbox("Week", weeks, index=weeks.index(default), key="rep_week",
                          format_func=lambda w: week_label(w) + (" (this week, so far)" if w == current else ""))

report_view, targets_view, history_view = st.tabs(["Report", "Targets", "History"])
with report_view:
    report_tab(chosen)
with targets_view:
    targets_tab()
with history_view:
    history_tab()
