"""GTM charts: the automatic GTM map (from Configuration's tracks and cadences, with live counts), the diagram builder
(templates + a plain-text outline) and the institute-category comparison — all downloadable as PNG, SVG or PDF."""

from __future__ import annotations

from datetime import date

import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Diagram, Track, User
from app.services.diagrams import (
    GROUPINGS,
    SEGMENT_METRICS,
    TEMPLATES,
    DiagramError,
    cadence_outline,
    comparison_chart,
    delete_diagram,
    describe,
    diagrams_frame,
    gtm_outline,
    parse_outline,
    period_range,
    render,
    save_diagram,
    segment_comparison,
)
from app.services.lead_queries import frame_to_xlsx
from app.services.money import inr
from app.ui.common import require_user
from app.ui.components import esc
from app.ui.layout import page_header

user = require_user()
FLASH = "edit_flash"
OUTLINE_KEY = "gtm_outline"
SYNTAX = """**How to write an outline**

| Type this | You get |
|---|---|
| `# Title` | the title at the top |
| `> Subtitle` | a line under the title |
| `## Heading` | a new section |
| `A -> B -> C` | boxes joined by arrows (one row per line) |
| `Box [small text]` | small text inside the box |
| `Box {12 at this step}` | a badge above the box |
| `note: …` | a caption under the section |
| `Parent:` then indented `  - A -> B` lines | a box that branches into several rows |
| `hub: Centre` then indented `  - Spoke [what they do]` lines | a hub with spokes |

Tip: start from a template, then change the words."""


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def _tracks() -> list[tuple[int, str]]:
    with session_scope() as session:
        return [(t.id, t.name) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                         .order_by(Track.sort_order))]


@st.cache_data(ttl=120, show_spinner="Drawing…")
def _gtm(track_ids: tuple[int, ...], live: bool) -> str:
    with session_scope() as session:
        return gtm_outline(session, list(track_ids) or None, live=live)


@st.cache_data(ttl=600, show_spinner=False, max_entries=64)
def _draw(outline: str, fmt: str) -> bytes:
    return render(parse_outline(outline), fmt)


def downloads(outline: str, stem: str, key: str) -> None:
    with st.container(horizontal=True, gap="small"):
        for fmt, icon in (("png", ":material/image:"), ("svg", ":material/polyline:"),
                          ("pdf", ":material/picture_as_pdf:")):
            st.download_button(fmt.upper(), data=_draw(outline, fmt), file_name=f"{stem}.{fmt}",
                               icon=icon, key=f"{key}_{fmt}", on_click="ignore",
                               mime={"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}[fmt])


def picture(png: bytes, alt: str, caption: str) -> None:
    """The image with alt text (st.image can't set one), plus a visible caption."""
    import base64

    st.markdown(f'<img src="data:image/png;base64,{base64.b64encode(png).decode()}" alt="{esc(alt)}" '
                f'style="width:100%;height:auto;border-radius:10px;background:#fff">', unsafe_allow_html=True)
    st.caption(caption)


def show(outline: str, caption: str) -> None:
    picture(_draw(outline, "png"), describe(parse_outline(outline)), caption)


# ---------------------------------------------------------------- GTM map


def map_tab() -> None:
    tracks = _tracks()
    names = dict(tracks)
    with st.container(horizontal=True, gap="small", vertical_alignment="bottom"):
        chosen = st.multiselect("Tracks", [tid for tid, _ in tracks], format_func=names.get,
                                placeholder="All active tracks", key="gtm_tracks")
        live = st.toggle("Live counts", value=True, key="gtm_live",
                         help="Badges show how many leads are at each step now, and how many are overdue.")
    st.caption("Drawn from the tracks and cadences in Configuration, so it changes when a cadence changes.")
    try:
        outline = _gtm(tuple(chosen), live)
    except DiagramError as error:
        st.info(str(error))
        return
    show(outline, "GTM map")
    downloads(outline, f"gtm-map-{date.today().isoformat()}", "map")
    if st.button("Edit a copy in the diagram builder", icon=":material/edit:", key="gtm_copy"):
        st.session_state[OUTLINE_KEY] = outline
        st.session_state["gtm_title"] = f"GTM map {date.today():%d %b %Y}"
        st.session_state["gtm_open_id"] = None
        st.session_state["gtm_kind"] = "gtm"
        st.toast("Copied into the Diagram builder tab.", icon=":material/content_copy:")


# ---------------------------------------------------------------- builder


def _load(outline: str, title: str, kind: str, diagram_id: int | None = None) -> None:
    st.session_state[OUTLINE_KEY] = outline
    st.session_state["gtm_title"] = title
    st.session_state["gtm_kind"] = kind
    st.session_state["gtm_open_id"] = diagram_id


def builder_tab() -> None:
    can = user.can("member")
    with session_scope() as session:
        saved = diagrams_frame(session)
    st.session_state.setdefault(OUTLINE_KEY, TEMPLATES["reseller"])
    st.session_state.setdefault("gtm_title", "Reseller model")
    st.session_state.setdefault("gtm_kind", "reseller")
    st.session_state.setdefault("gtm_open_id", None)

    with st.container(horizontal=True, gap="small", vertical_alignment="bottom"):
        with st.popover("Start from a template", icon=":material/dashboard_customize:"):
            if st.button("Partner models", width="stretch", key="tpl_partner"):
                _load(TEMPLATES["partner"], "Partner models", "partner")
                st.rerun()
            if st.button("Reseller model", width="stretch", key="tpl_reseller"):
                _load(TEMPLATES["reseller"], "Reseller model", "reseller")
                st.rerun()
            tracks = _tracks()
            track = st.selectbox("Cadence flow of a track", [tid for tid, _ in tracks], format_func=dict(tracks).get,
                                 key="tpl_track")
            if st.button("Use this track's cadence", width="stretch", key="tpl_cadence"):
                with session_scope() as session:
                    text = cadence_outline(session, int(track))
                _load(text, f"{dict(tracks)[track]} cadence", "cadence")
                st.rerun()
            if st.button("Blank", width="stretch", key="tpl_blank"):
                _load(TEMPLATES["blank"], "My diagram", "custom")
                st.rerun()
        if not saved.empty:
            with st.popover(f"Open saved · {len(saved)}", icon=":material/folder_open:"):
                pick = st.selectbox("Diagram", saved["id"].tolist(),
                                    format_func=dict(zip(saved["id"], saved["Title"])).get, key="open_pick")
                if st.button("Open", type="primary", key="open_go"):
                    with session_scope() as session:
                        d = session.get(Diagram, int(pick))
                        _load(d.outline, d.title, d.kind, d.id)
                    st.rerun()
        with st.popover("How to write it", icon=":material/help:"):
            st.markdown(SYNTAX)

    left, right = st.columns([1, 2], gap="large")
    with left:
        title = st.text_input("Title", key="gtm_title")
        outline = st.text_area("Outline", key=OUTLINE_KEY, height=380,
                               help="Press Ctrl+Enter (or click outside the box) to redraw.")
        open_id = st.session_state.get("gtm_open_id")
        with st.container(horizontal=True, gap="small"):
            if st.button("Save" if open_id else "Save diagram", type="primary", icon=":material/save:",
                         disabled=not can, key="gtm_save"):
                try:
                    with session_scope() as session:
                        d = save_diagram(session, title=title, outline=outline, user=session.get(User, user.id),
                                         kind=st.session_state.get("gtm_kind", "custom"), diagram_id=open_id)
                        st.session_state["gtm_open_id"] = d.id
                except (DiagramError, PermissionError) as error:
                    st.error(str(error))
                else:
                    _done(f"Saved “{title}”.")
            if open_id and st.button("Save as new", disabled=not can, key="gtm_save_new"):
                try:
                    with session_scope() as session:
                        d = save_diagram(session, title=f"{title} (copy)" if title else "Copy", outline=outline,
                                         user=session.get(User, user.id),
                                         kind=st.session_state.get("gtm_kind", "custom"))
                        new_id, new_title = d.id, d.title
                except (DiagramError, PermissionError) as error:
                    st.error(str(error))
                else:
                    st.session_state["gtm_open_id"] = new_id
                    st.session_state["gtm_pending_title"] = new_title
                    _done(f"Saved a copy: “{new_title}”.")
            if open_id:
                with st.popover("Delete", type="tertiary", disabled=not can):
                    if st.button("Delete this diagram", key="gtm_delete"):
                        try:
                            with session_scope() as session:
                                name = delete_diagram(session, int(open_id), session.get(User, user.id))
                        except (DiagramError, PermissionError) as error:
                            st.error(str(error))
                        else:
                            st.session_state["gtm_open_id"] = None
                            _done(f"Deleted “{name}”.")
        st.caption("Editing a saved diagram." if open_id else "Not saved yet.")
    with right:
        try:
            parse_outline(outline)
        except DiagramError as error:
            st.warning(str(error), icon=":material/edit_note:")
            return
        show(outline, title or "Diagram")
        downloads(outline, (title or "diagram").strip().replace(" ", "-").lower()[:60], "builder")


# ---------------------------------------------------------------- comparison


@st.cache_data(ttl=120, show_spinner="Working out the numbers…")
def _comparison(period: str, grouping: str):
    start, end = period_range(period)
    with session_scope() as session:
        return segment_comparison(session, start, end, GROUPINGS[grouping]), start, end


def comparison_tab() -> None:
    with st.container(horizontal=True, gap="small", vertical_alignment="bottom"):
        period = st.selectbox("Period", ["This FY", "Last FY", "This quarter", "Last 90 days", "All time"],
                              key="cmp_period")
        grouping = st.selectbox("Compare", list(GROUPINGS), key="cmp_group")
        metrics = st.multiselect("Measures", list(SEGMENT_METRICS),
                                 default=["Leads", "Contacted", "Replied", "Demos", "Won", "Won value (₹)"],
                                 key="cmp_metrics")
    frame, start, end = _comparison(period, grouping)
    st.caption(f"Public/private comes from the institution's management; urban/rural from its area. Leads = all "
               f"leads now; everything else = {start:%d %b %Y} to {end:%d %b %Y}. Institutions without these "
               "details are under Not set — fill them in on Colleges to sharpen the picture.")
    if frame.empty:
        st.info("No leads yet.")
        return
    title = f"Institute categories — {period}"
    png = comparison_chart(frame, metrics, title, "png")
    picture(png, f"{title}: bar charts of {', '.join(metrics)} for each category. The same numbers are in the "
                 "table below.", title)
    shown = frame.copy()
    shown["Won value (₹)"] = shown["Won value (₹)"].map(lambda v: inr(v, decimals=False))
    shown["Win rate"] = shown["Win rate"].map(lambda v: f"{v:.0%}")
    st.dataframe(shown, hide_index=True, width="stretch")
    stem = f"institute-categories-{date.today().isoformat()}"
    with st.container(horizontal=True, gap="small"):
        st.download_button("PNG", data=png, file_name=f"{stem}.png", icon=":material/image:", key="cmp_png",
                           on_click="ignore", mime="image/png")
        st.download_button("SVG", data=comparison_chart(frame, metrics, title, "svg"), file_name=f"{stem}.svg",
                           icon=":material/polyline:", key="cmp_svg", on_click="ignore", mime="image/svg+xml")
        st.download_button("PDF", data=comparison_chart(frame, metrics, title, "pdf"), file_name=f"{stem}.pdf",
                           icon=":material/picture_as_pdf:", key="cmp_pdf", on_click="ignore",
                           mime="application/pdf")
        st.download_button("Excel", data=frame_to_xlsx(frame, "Categories"), file_name=f"{stem}.xlsx",
                           icon=":material/table_view:", key="cmp_xlsx", on_click="ignore")


# ---------------------------------------------------------------- page

if "gtm_pending_title" in st.session_state:  # a widget's value can only be set before it is drawn
    st.session_state["gtm_title"] = st.session_state.pop("gtm_pending_title")

page_header("GTM charts", "The go-to-market map drawn from your cadences, diagrams for partner and reseller models, "
            "and how institute categories compare. Download any of them as PNG, SVG or PDF.", eyebrow="Marketing")
map_view, builder_view, compare_view = st.tabs(["GTM map", "Diagram builder", "Institute categories"])
with map_view:
    map_tab()
with builder_view:
    builder_tab()
with compare_view:
    comparison_tab()
