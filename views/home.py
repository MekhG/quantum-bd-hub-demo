"""Dashboard: this period at a glance — headline numbers with trends, the pipeline, what needs attention,
upcoming demos, team activity and reply rates."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from app.db import session_scope
from app.services.activity_log import ghost_frame, overdue_count
from app.services.dashboard_metrics import (
    PERIODS,
    expiring_quotes,
    lost_reasons,
    won_lost_by_month,
    kpis,
    period_bounds,
    team_activity,
    upcoming_demos,
    weekly_labels,
    weekly_series,
)
from app.services.dashboard_queries import load_dashboard
from app.services.lead_queries import import_history
from app.services.money import inr
from app.services.report_metrics import pipeline_now
from app.services.content import waiting_approval_count
from app.services.partners import pending_count
from app.services.postsale import renewals_due
from app.services.privacy_requests import open_counts
from app.services.signals import hot_leads, hot_score
from app.services.queries import queries_frame
from app.services.tasks import reply_rate_frame
from app.ui.common import require_user
from app.ui.components import card, channel_tile, chip, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
# Deltas compare the same number of days in the previous period (e.g. Mon–Tue vs last Mon–Tue).
_COMPARE = {"This week": "last week", "This month": "last month", "This quarter": "last quarter",
            "This FY": "last FY"}


@st.cache_data(ttl=60, show_spinner=False)
def _load(period_label: str):
    period = period_bounds(period_label)
    with session_scope() as session:
        queries = queries_frame(session, days=30)
        return {
            "dash": load_dashboard(session),
            "kpis": kpis(session, period),
            "series": weekly_series(session),
            "team": team_activity(session, period),
            "demos": upcoming_demos(session),
            "rates": reply_rate_frame(session),
            "due": overdue_count(session, None),
            "ghosts": len(ghost_frame(session, owner_id=None)),
            "late_queries": int((queries["Status"] == "Reply overdue").sum()) if not queries.empty else 0,
            "recent": import_history(session, limit=5),
            "pipeline": pipeline_now(session),
            "hot": hot_leads(session, limit=6),
            "hot_score": hot_score(session),
            "expiring": expiring_quotes(session),
            "won_lost": won_lost_by_month(session),
            "reasons": lost_reasons(session),
            "registrations": pending_count(session),
            "posts_waiting": waiting_approval_count(session),
            "renewals": renewals_due(session),
            "privacy": open_counts(session),
        }


head, tools = st.columns([3, 2], vertical_alignment="bottom")
with tools:
    period_label = st.radio("Period", PERIODS, horizontal=True, key="dash_period", label_visibility="collapsed")
with head:
    page_header("Dashboard", "The pipeline and the team's work at a glance.", eyebrow="Overview")
data = _load(period_label)
dash = data["dash"]

if dash.stages.empty:
    st.info("The configuration tables are empty. In the terminal run:  python -m app.seed.seed_config")
    st.stop()
if dash.open_leads + dash.clients == 0:
    empty_state("No leads yet — import a tracker to fill the pipeline.", page="views/import_data.py",
                label="Go to Import", icon=":material/upload_file:")
    st.stop()

# ---------------------------------------------------------------- headline numbers
tiles = st.columns(len(data["kpis"]))
for col, k in zip(tiles, data["kpis"]):
    col.metric(k["label"], f"{k['now']:,}", delta=k["now"] - k["before"], border=True, help=k["help"],
               delta_description=f"vs {_COMPARE[period_label]}")
st.caption(f"Open leads now: **{dash.open_leads:,}** · clients: **{dash.clients:,}** · "
           f"contacts: **{dash.contacts:,}**.")

# ---------------------------------------------------------------- money, hot leads, expiring quotes
total, weighted, open_quotes = data["pipeline"]
m1, m2, m3 = st.columns([1, 1.15, 1.15], gap="medium")
with m1, card("dash-pipeline"):
    st.markdown("**Pipeline**")
    html_block(f'<div style="font-size:1.6rem;font-weight:600">{esc(inr(total, decimals=False))}</div>'
               f'<div class="qa-small qa-muted">{open_quotes} open quotation(s), excluding GST</div>'
               f'<div style="margin-top:8px"><b>{esc(inr(weighted, decimals=False))}</b>'
               f'<span class="qa-small qa-muted"> weighted by each lead\'s potential</span></div>')
    st.page_link("views/quotes.py", label="All quotes", icon=":material/request_quote:")
with m2, card("dash-hot"):
    hot_label = f"score {data['hot_score']}+"
    html_block(f'<div style="display:flex;justify-content:space-between;align-items:center"><b>Hot leads</b>'
               f'{chip(hot_label, "yellow")}</div>')
    if data["hot"].empty:
        st.caption("None yet. Save signals from News & signals to raise scores.")
    for row in data["hot"].to_dict("records"):
        st.page_link("views/leads.py", label=f"{row['Institution']} · {row['Score']}",
                     query_params={"lead": int(row["lead_id"])}, icon=":material/local_fire_department:")
with m3, card("dash-expiring"):
    st.markdown("**Quotes expiring in 7 days**")
    if data["expiring"].empty:
        st.caption("No sent quotation runs out this week.")
    for row in data["expiring"].to_dict("records"):
        st.page_link("views/leads.py", label=f"{row['Institution']} · {row['Valid until']:%d %b}",
                     query_params={"lead": int(row["lead_id"])}, icon=":material/timer:",
                     help=f"{row['Quote']} · {inr(row['Value'], decimals=False)} excl. GST")

# ---------------------------------------------------------------- pipeline + attention
left, right = st.columns([1.2, 1], gap="large")
with left, card("pipeline"):
    st.markdown("**Pipeline by stage**")
    st.caption("Leads currently at each stage, all tracks.")
    chart_data = dash.stages[["Stage", "Leads", "sort_order"]]
    # Altair text doesn't follow Streamlit's theme, so pick the ink for the active mode.
    ink = "#F3EADB" if st.context.theme.type == "dark" else "#2B1C10"
    bars = (
        alt.Chart(chart_data)
        .mark_bar(cornerRadiusEnd=4, height={"band": 0.62})
        .encode(
            y=alt.Y("Stage:N", sort=alt.SortField("sort_order"), title=None,
                    axis=alt.Axis(labelLimit=180, labelColor=ink, labelFontSize=12, domain=False, ticks=False)),
            x=alt.X("Leads:Q", title=None, axis=alt.Axis(grid=True, tickCount=4, labels=False, ticks=False,
                                                          domain=False)),
            tooltip=[alt.Tooltip("Stage:N"), alt.Tooltip("Leads:Q", format=",")],
        )
    )
    labels = bars.mark_text(align="left", dx=6, fontWeight=600, color=ink).encode(
        text=alt.Text("Leads:Q", format=","))
    st.altair_chart((bars + labels).properties(height=32 * len(chart_data)), width="stretch")

with right:
    with card("attention"):
        st.markdown("**Needs attention**")
        items = [
            ("web", "Queries past the 1-day reply target", data["late_queries"], "views/queries.py"),
            ("task", "Tasks due today or overdue (team)", data["due"], "views/today.py"),
            ("email", "Contacts waiting for verification", dash.unverified_contacts, "views/contacts.py"),
            ("internal", "Cadences finished with no reply", data["ghosts"], "views/today.py"),
            ("phone", "Open leads with nothing logged yet", dash.leads_without_activity, "views/leads.py"),
            ("meeting", "Partner deal registrations to decide", data["registrations"], "views/partners.py"),
            ("linkedin", "Posts waiting for approval", data["posts_waiting"], "views/content.py"),
            ("demo", "Warranties / AMCs ending in 90 days", data["renewals"], "views/clients.py"),
        ]
        privacy_open, privacy_late = data["privacy"]
        if privacy_open:
            items.append(("web", f"Privacy requests open ({privacy_late} overdue)" if privacy_late
                          else "Privacy requests open", privacy_open, "views/privacy.py"))
        for channel, label, n, page in items:
            row, link = st.columns([5, 1.25], vertical_alignment="center")
            urgent = channel in ("web", "task") or (channel == "meeting" and user.can("admin"))
            tone = "orange" if n and urgent else "neutral"
            row.html(f'<div class="qa-row" style="gap:10px;align-items:center">{channel_tile(channel, 30)}'
                     f'<span style="flex:1">{esc(label)}</span>{chip(f"{n:,}", tone, strong=True)}</div>')
            link.page_link(page, label="Open")
# ---------------------------------------------------------------- trend + demos
w1, w2 = st.columns([1.2, 1], gap="large")
with w1, card("trend"):
    st.markdown("**Outreach and replies — last 8 weeks**")
    series = data["series"]
    weeks = weekly_labels(len(series["Outreach done"]))
    trend = pd.DataFrame({"Week": weeks * 2,
                          "Count": series["Outreach done"] + series["Replies"],
                          "Series": ["Outreach done"] * len(weeks) + ["Replies"] * len(weeks),
                          "order": list(range(len(weeks))) * 2})
    ink = "#F3EADB" if st.context.theme.type == "dark" else "#2B1C10"
    grid = "#3D3026" if st.context.theme.type == "dark" else "#EDE3D6"
    # Two series on one count axis; colours are the validated palette's first two slots, and the legend plus
    # end labels carry identity so colour is never the only cue.
    palette = ["#D29B65", "#4FB0A8"] if st.context.theme.type == "dark" else ["#8B5A2B", "#2F7F7A"]
    base = alt.Chart(trend).encode(
        x=alt.X("Week:N", sort=alt.SortField("order"), title=None,
                axis=alt.Axis(labelColor=ink, labelAngle=0, domain=False, ticks=False)),
        y=alt.Y("Count:Q", title=None, axis=alt.Axis(labelColor=ink, gridColor=grid, domain=False, ticks=False,
                                                     tickCount=4)),
        color=alt.Color("Series:N", scale=alt.Scale(domain=["Outreach done", "Replies"], range=palette),
                        legend=alt.Legend(orient="top", title=None, labelColor=ink)),
        tooltip=["Week:N", "Series:N", alt.Tooltip("Count:Q", format=",")],
    )
    chart = base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)
    st.altair_chart(chart.properties(height=230), width="stretch")
with w2, card("demos"):
    st.markdown("**Demos and meetings — next 14 days**")
    demos = data["demos"]
    if demos.empty:
        st.caption("Nothing booked yet.")
    for row in demos.head(6).to_dict("records"):
        when, lead = row["When"], row["lead_id"]
        label = f"**{when.day} {when:%b}, {when:%I:%M %p}**".replace(", 0", ", ") + \
            f" · {row['Institution'] or row['What']}"
        st.page_link("views/leads.py", label=label, icon=":material/co_present:",
                     query_params={"lead": int(lead)} if lead is not None and lead == lead else None)
        st.caption(f"{row['What']} · {row['Who']}")

# ---------------------------------------------------------------- team + reply rates
t1, t2 = st.columns([1.2, 1], gap="large")
with t1, card("team"):
    st.markdown(f"**Team activity — {period_label.lower()}**")
    team = data["team"]
    st.dataframe(team, hide_index=True, width="stretch",
                 column_config={
                     "Total": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                              max_value=max(int(team["Total"].max()), 1)),
                     "Overdue now": st.column_config.NumberColumn(
                         help="Tasks past their due-by date right now (same rule as Today)."),
                 })
with t2, card("rates"):
    st.markdown("**Reply rate by track**")
    st.caption("Of the leads we contacted, how many wrote back. Replies, not opens — the hub doesn't track opens.")
    st.dataframe(data["rates"], hide_index=True, width="stretch",
                 column_config={
                     "Track": st.column_config.TextColumn(width="medium"),
                     "Contacted": st.column_config.NumberColumn(format="%d", width="small"),
                     "Replied": st.column_config.NumberColumn(format="%d", width="small"),
                     "Reply rate": st.column_config.ProgressColumn(format="percent", min_value=0.0, max_value=1.0),
                 })

# ---------------------------------------------------------------- won and lost
o1, o2 = st.columns([1.2, 1], gap="large")
with o1, card("won-lost"):
    st.markdown("**Won and lost — last 6 months**")
    st.caption("Won = purchase orders by PO date. Lost = leads moved to a lost stage.")
    frame = data["won_lost"]
    ink = "#F3EADB" if st.context.theme.type == "dark" else "#2B1C10"
    grid = "#3D3026" if st.context.theme.type == "dark" else "#EDE3D6"
    palette = ["#D29B65", "#4FB0A8"] if st.context.theme.type == "dark" else ["#8B5A2B", "#2F7F7A"]
    chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=3).encode(
        x=alt.X("Month:N", sort=alt.SortField("order"), title=None,
                axis=alt.Axis(labelColor=ink, labelAngle=0, domain=False, ticks=False)),
        xOffset=alt.XOffset("Outcome:N", sort=["Won", "Lost"]),
        y=alt.Y("Count:Q", title=None, axis=alt.Axis(labelColor=ink, gridColor=grid, domain=False, ticks=False,
                                                     tickCount=4, format="d")),
        color=alt.Color("Outcome:N", scale=alt.Scale(domain=["Won", "Lost"], range=palette),
                        legend=alt.Legend(orient="top", title=None, labelColor=ink)),
        tooltip=["Month:N", "Outcome:N", alt.Tooltip("Count:Q", format=",")])
    if int(frame["Count"].sum()) == 0:
        st.caption("Nothing won or lost in the last six months yet.")
    else:
        st.altair_chart(chart.properties(height=220), width="stretch")
with o2, card("lost-reasons"):
    st.markdown("**Why we lose**")
    reasons = data["reasons"]
    if reasons.empty:
        st.caption("No lost leads yet.")
    else:
        st.dataframe(reasons, hide_index=True, width="stretch",
                     column_config={"Leads": st.column_config.ProgressColumn(
                         format="%d", min_value=0, max_value=max(int(reasons["Leads"].max()), 1))})
    st.page_link("views/report.py", label="Weekly report", icon=":material/summarize:")

with st.expander("Leads by track and recent imports", icon=":material/table_view:"):
    a, b = st.columns(2, gap="large")
    a.dataframe(dash.tracks, hide_index=True, width="stretch",
                column_config={
                    "Leads": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                             max_value=max(int(dash.tracks["Leads"].max()), 1)),
                    "Clients": st.column_config.NumberColumn(format="%d"),
                    "NQM": st.column_config.NumberColumn(format="%d", help="NQM applicants in this track"),
                })
    recent = data["recent"]
    if recent.empty:
        b.caption("No imports yet.")
    else:
        b.dataframe(recent[["When (IST)", "Kind", "File", "By", "Rows", "Result"]], hide_index=True, width="stretch",
                    column_config={"When (IST)": st.column_config.DatetimeColumn(format="DD MMM, HH:mm"),
                                   "Rows": st.column_config.NumberColumn(format="%d")})
