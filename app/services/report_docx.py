"""The weekly report as a Word file on the Demo letterhead (header and footer taken from the quotation
template), with charts drawn by matplotlib; and the same numbers as an Excel workbook."""

from __future__ import annotations

import io

import pandas as pd
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.services.money import inr

FONT = "Cambria"           # the team's documents use Cambria
INK = RGBColor(0x2B, 0x1C, 0x10)
MUTED = RGBColor(0x7A, 0x65, 0x55)
BLUE, ORANGE = "#8B5A2B", "#2F7F7A"   # brand brown + teal: the categorical pair used across the hub
HEADER_FILL = "F2E6D6"


# ---------------------------------------------------------------- charts


def _plt():
    import matplotlib

    matplotlib.use("Agg")  # no display on the server
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.edgecolor": "#D8C9B6", "axes.labelcolor": "#2B1C10",
                         "xtick.color": "#5A4636", "ytick.color": "#5A4636"})
    return plt


def _png(fig) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, bbox_inches="tight")
    _plt().close(fig)
    return buffer.getvalue()


def stage_chart(stages: list[dict]) -> bytes:
    plt = _plt()
    rows = [s for s in stages]
    names = [s["Stage"] for s in rows][::-1]
    values = [s["Leads"] for s in rows][::-1]
    colours = [{"won": "#16A34A", "lost": "#DC2626", "nurture": ORANGE}.get(s["outcome"], BLUE) for s in rows][::-1]
    fig, ax = plt.subplots(figsize=(6.2, 0.32 * len(rows) + 0.6))
    bars = ax.barh(names, values, color=colours, height=0.62)
    for bar, value, stage in zip(bars, values, rows[::-1]):
        moved = stage["Moved in this week"]
        label = f"{value:,}" + (f"  (+{moved} this week)" if moved else "")
        ax.text(bar.get_width() + max(max(values), 1) * 0.01, bar.get_y() + bar.get_height() / 2, label,
                va="center", fontsize=8, color="#2B1C10")
    ax.set_xlim(0, max(max(values), 1) * 1.3)
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    return _png(fig)


def followup_chart(followups: list[dict]) -> bytes:
    """Follow-ups by channel, this week against last week (horizontal, so one channel or five both look right)."""
    plt = _plt()
    rows = followups or [{"Channel": "Calls", "This week": 0, "Last week": 0},
                         {"Channel": "Emails", "This week": 0, "Last week": 0}]
    names = [f["Channel"] for f in rows][::-1]
    now = [f["This week"] for f in rows][::-1]
    before = [f["Last week"] for f in rows][::-1]
    y = list(range(len(names)))
    fig, ax = plt.subplots(figsize=(6.2, 0.55 * len(names) + 0.7))
    height = 0.36
    top = max(now + before + [1])
    for offset, values, colour, label in ((height / 2, before, ORANGE, "Last week"),
                                          (-height / 2, now, BLUE, "This week")):
        bars = ax.barh([i + offset for i in y], values, height, color=colour, label=label)
        for bar, value in zip(bars, values):
            ax.text(bar.get_width() + top * 0.01, bar.get_y() + bar.get_height() / 2, f"{value:,}", va="center",
                    fontsize=8)
    ax.set_yticks(y, names)
    ax.set_xlim(0, top * 1.15)
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.legend(frameon=False, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2)
    return _png(fig)


def trend_chart(trend: dict) -> bytes:
    plt = _plt()
    labels = trend["labels"]
    fig, ax = plt.subplots(figsize=(6.2, 2.4))
    for name, colour, style in (("Outreach", BLUE, "-"), ("Follow-ups", ORANGE, "-"), ("Replies", "#16A34A", "--")):
        values = trend[name]
        ax.plot(labels, values, style, color=colour, marker="o", markersize=3.5, linewidth=1.8, label=name)
        ax.annotate(f"{values[-1]:,}", (len(labels) - 1, values[-1]), textcoords="offset points", xytext=(6, 0),
                    va="center", fontsize=8, color=colour)
    from matplotlib.ticker import MaxNLocator

    ax.grid(axis="y", color="#EDE3D6", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))  # counts: no 0.5 ticks
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=8, ncol=3)
    ax.set_ylim(bottom=0)
    return _png(fig)


def segment_chart(segments: list[dict]) -> bytes:
    plt = _plt()
    names = [s["Segment"] for s in segments]
    contacted = [s["Contacted (week)"] for s in segments]
    replied = [s["Replied (week)"] for s in segments]
    x = range(len(names))
    fig, ax = plt.subplots(figsize=(6.2, 2.4))
    width = 0.38
    ax.bar([i - width / 2 for i in x], contacted, width, color=BLUE, label="Contacted this week")
    ax.bar([i + width / 2 for i in x], replied, width, color="#16A34A", label="Replied this week")
    from matplotlib.ticker import MaxNLocator

    ax.set_xticks(list(x), names, fontsize=8)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(frameon=False, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2)
    ax.grid(axis="y", color="#EDE3D6", linewidth=0.8)
    ax.set_axisbelow(True)
    return _png(fig)


# ---------------------------------------------------------------- Word helpers


def _clear_body(doc: Document) -> None:
    """Keeps the letterhead (header/footer live in the section properties) and removes the quotation's text."""
    body = doc.element.body
    for child in list(body.iterchildren()):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _run(paragraph, text: str, *, size: float = 10.5, bold: bool = False, colour: RGBColor | None = None,
         italic: bool = False):
    run = paragraph.add_run(text)
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    if colour is not None:
        run.font.color.rgb = colour
    return run


def _para(doc: Document, text: str = "", *, size: float = 10.5, bold: bool = False, colour: RGBColor | None = None,
          space_after: float = 4, align=None, italic: bool = False):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.space_before = Pt(0)
    if align is not None:
        paragraph.alignment = align
    if text:
        _run(paragraph, text, size=size, bold=bold, colour=colour, italic=italic)
    return paragraph


def _heading(doc: Document, text: str) -> None:
    paragraph = _para(doc, text, size=13, bold=True, colour=INK, space_after=4)
    paragraph.paragraph_format.space_before = Pt(10)
    paragraph.paragraph_format.keep_with_next = True


def _shade(cell, fill: str) -> None:
    properties = cell._element.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    properties.append(shading)


def _borders(table) -> None:
    properties = table._element.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), "D8C9B6")
        borders.append(element)
    properties.append(borders)


def _table(doc: Document, frame: pd.DataFrame, *, money: tuple[str, ...] = (), percent: tuple[str, ...] = (),
           widths: list[float] | None = None) -> None:
    table = doc.add_table(rows=1, cols=len(frame.columns))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(table)
    for cell, column in zip(table.rows[0].cells, frame.columns):
        _shade(cell, HEADER_FILL)
        cell.paragraphs[0].paragraph_format.space_after = Pt(0)
        _run(cell.paragraphs[0], str(column), size=8.5, bold=True)
    for record in frame.itertuples(index=False):
        cells = table.add_row().cells
        for cell, column, value in zip(cells, frame.columns, record):
            if column in money:
                text = inr(value or 0, decimals=False)
            elif column in percent:
                text = f"{float(value or 0):.0%}"
            elif isinstance(value, float) and value.is_integer():
                text = f"{int(value):,}"
            elif isinstance(value, int):
                text = f"{value:,}"
            else:
                text = "" if value is None or value != value else str(value)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            if isinstance(value, (int, float)) and column not in ("Note",):
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _run(paragraph, text, size=8.5)
    if widths:
        for row in table.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = Cm(width)
    _para(doc, space_after=2)


def _picture(doc: Document, png: bytes, width_cm: float = 15.5) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.add_run().add_picture(io.BytesIO(png), width=Cm(width_cm))


def _delta(now: int, before: int) -> str:
    diff = now - before
    if diff == 0:
        return "same as last week"
    return f"{'▲' if diff > 0 else '▼'} {abs(diff):,} vs last week"


def _bullets(doc: Document, text: str | None, empty: str) -> None:
    lines = [line.strip(" -•\t") for line in (text or "").splitlines() if line.strip(" -•\t")]
    if not lines:
        _para(doc, empty, colour=MUTED, italic=True)
        return
    for line in lines:
        paragraph = _para(doc, space_after=2)
        paragraph.paragraph_format.left_indent = Cm(0.5)
        paragraph.paragraph_format.first_line_indent = Cm(-0.4)
        _run(paragraph, "•  " + line)


# ---------------------------------------------------------------- the Word report


def report_docx(report: dict, *, template: bytes, comments: dict[str, str | None], prepared_by: str,
                status: str) -> bytes:
    doc = Document(io.BytesIO(template))
    _clear_body(doc)

    _para(doc, "Weekly BD & Marketing Report", size=20, bold=True, colour=INK, space_after=2)
    _para(doc, f"Demo · Week of {report['label']}" + (" (so far)" if report.get("is_current") else ""),
          size=12, colour=INK, space_after=2)
    _para(doc, f"Prepared by {prepared_by} · numbers as of {report['generated_at']} IST · "
               f"{'Final' if status == 'final' else 'Draft'}", size=9, colour=MUTED, space_after=8)

    # 1. Headline numbers: two rows of four tiles.
    _heading(doc, "1. This week at a glance")
    tiles = report["headline"]
    table = doc.add_table(rows=2, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(table)
    for i, tile in enumerate(tiles[:8]):
        cell = table.rows[i // 4].cells[i % 4]
        _shade(cell, "FAF5EE")
        top = cell.paragraphs[0]
        top.paragraph_format.space_after = Pt(0)
        _run(top, tile["label"], size=8.5, colour=MUTED)
        middle = cell.add_paragraph()
        middle.paragraph_format.space_after = Pt(0)
        _run(middle, f"{tile['now']:,}", size=18, bold=True, colour=INK)
        bottom = cell.add_paragraph()
        bottom.paragraph_format.space_after = Pt(2)
        _run(bottom, _delta(tile["now"], tile["before"]), size=8, colour=MUTED)
    _para(doc, space_after=2)
    totals = report["totals"]
    rate = totals.get("reply_rate")
    lines = [
        f"Pipeline: {inr(totals['pipeline'], decimals=False)} in {totals['open_quotes']} open quotation(s), excluding "
        f"GST (weighted by potential: {inr(totals['pipeline_weighted'], decimals=False)}).",
        f"Quotations sent this week: {inr(totals['quotes_value'], decimals=False)}. Deals won this week: "
        f"{inr(totals['won_value'], decimals=False)}.",
    ]
    if rate is not None:
        lines.append(f"Reply rate: {rate:.0%} of the {totals['reply_cohort']} organisations first contacted in the "
                     "last four weeks have written back.")
    lines.append(f"New signals saved: {totals['new_signals']}.")
    for line in lines:
        _para(doc, line, size=10, space_after=2)

    # 2. Outreach and follow-ups.
    _heading(doc, "2. Outreach and follow-ups")
    _para(doc, f"{tiles[0]['now']:,} organisation(s) contacted for the first time ({totals['outreach_messages']:,} "
               f"message(s) and calls on their first day) and {totals['followups']:,} follow-up(s), against "
               f"{totals['followups_before']:,} last week.", size=10)
    _picture(doc, followup_chart(report["followups"]))
    _para(doc, "Last eight weeks", size=9, bold=True, colour=MUTED, space_after=0)
    _picture(doc, trend_chart(report["trend"]))

    # 3. Leads by stage.
    _heading(doc, "3. Leads by stage (end of the week)")
    _picture(doc, stage_chart(report["stages"]))

    # 4. Deals.
    _heading(doc, "4. Deals closed")
    if report["won"]:
        _para(doc, "Won", size=10, bold=True, space_after=2)
        _table(doc, pd.DataFrame(report["won"]), money=("Value (₹)",))
    else:
        _para(doc, "No purchase orders this week.", colour=MUTED, italic=True)
    if report["lost"]:
        _para(doc, "Lost", size=10, bold=True, space_after=2)
        _table(doc, pd.DataFrame(report["lost"]))

    # 5. Team and targets.
    _heading(doc, "5. Team")
    if report["people"]:
        _table(doc, pd.DataFrame(report["people"]))
    if report["targets"]:
        _para(doc, "Targets vs actual", size=10, bold=True, space_after=2)
        _table(doc, pd.DataFrame(report["targets"]), percent=("Progress",))

    # 6. Segments.
    if report["segments"]:
        _heading(doc, "6. Public and private, urban and rural")
        _picture(doc, segment_chart(report["segments"]))
        frame = pd.DataFrame(report["segments"])
        _table(doc, frame, money=("Won value (FY, ₹)", "Pipeline now (₹)"))

    # 7. Hot leads.
    if report["hot"]:
        _heading(doc, "7. Hot leads")
        _table(doc, pd.DataFrame(report["hot"]))

    # Events & community (Week 9; missing from reports finalised earlier).
    community = report.get("community")
    if community and (community["events"] or community["new_members"]):
        _heading(doc, "Events and community")
        _para(doc, f"{len(community['events'])} event(s) this week · {community['new_members']} new community "
                   f"member(s) · {community['members']:,} in the community ({community['mailable']:,} mailable).",
              size=10)
        if community["events"]:
            _table(doc, pd.DataFrame(community["events"]))
        if community["top_institutions"]:
            _table(doc, pd.DataFrame(community["top_institutions"]))

    # LinkedIn / Instagram (added 2026-10-21; missing from reports finalised earlier).
    content = report.get("content")
    if content and (content["posted"] or content["themes"]):
        _heading(doc, "LinkedIn and Instagram")
        _para(doc, f"{content['posted']} post(s) this week (LinkedIn {content['linkedin']}, Instagram "
                   f"{content['instagram']}) · {content['approved_next_week']} approved for next week · "
                   f"{content['waiting_approval']} waiting for approval.", size=10)
        if content["best_post"]:
            best = content["best_post"]
            _para(doc, f"Best post so far: “{best['Post']}” ({best['Theme']}) — {best['Engagements']} engagements.",
                  size=10)
        if content["themes"]:
            _para(doc, "Which themes do best (last 12 weeks; engagements = likes + comments + reposts)", size=10,
                  bold=True, space_after=2)
            _table(doc, pd.DataFrame(content["themes"]))

    # 8. Comments.
    _heading(doc, "8. Comments")
    for title, key, empty in (("Highlights", "highlights", "None added."),
                              ("Risks and blockers", "risks", "None added."),
                              ("Support needed", "asks", "None added.")):
        _para(doc, title, size=10.5, bold=True, space_after=2)
        _bullets(doc, comments.get(key), empty)
    if report["notes"]:
        _para(doc, "From the team", size=10.5, bold=True, space_after=2)
        for note in report["notes"]:
            paragraph = _para(doc, space_after=3)
            _run(paragraph, f"{note['Person']}: ", bold=True)
            _run(paragraph, note["Note"])

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------- Excel


def report_xlsx(report: dict, comments: dict[str, str | None]) -> bytes:
    sheets: dict[str, pd.DataFrame] = {
        "Summary": pd.DataFrame([{"Metric": t["label"], "This week": t["now"], "Last week": t["before"],
                                  "What it counts": t["help"]} for t in report["headline"]]),
        "Follow-ups": pd.DataFrame(report["followups"]),
        "Leads by stage": pd.DataFrame(report["stages"]).drop(columns=["outcome"], errors="ignore"),
        "Deals won": pd.DataFrame(report["won"]),
        "Deals lost": pd.DataFrame(report["lost"]),
        "Team": pd.DataFrame(report["people"]),
        "Targets": pd.DataFrame(report["targets"]),
        "Segments": pd.DataFrame(report["segments"]),
        "Community": pd.DataFrame((report.get("community") or {}).get("events", [])),
        "Content themes": pd.DataFrame((report.get("content") or {}).get("themes", [])),
        "Trend": pd.DataFrame({"Week of": report["trend"]["labels"], "Outreach": report["trend"]["Outreach"],
                               "Follow-ups": report["trend"]["Follow-ups"], "Replies": report["trend"]["Replies"]}),
        "Comments": pd.DataFrame([{"Section": k.title(), "Text": comments.get(k) or ""}
                                  for k in ("highlights", "risks", "asks")]
                                 + [{"Section": f"Note — {n['Person']}", "Text": n["Note"]} for n in report["notes"]]),
    }
    totals = report["totals"]
    extra = pd.DataFrame([{"Metric": "Pipeline (excl. GST, ₹)", "This week": totals["pipeline"]},
                          {"Metric": "Pipeline weighted by potential (₹)", "This week": totals["pipeline_weighted"]},
                          {"Metric": "Quotations sent (₹)", "This week": totals["quotes_value"]},
                          {"Metric": "Deals won (₹)", "This week": totals["won_value"]},
                          {"Metric": "Reply rate (4-week cohort)", "This week": totals.get("reply_rate")}])
    sheets["Summary"] = pd.concat([sheets["Summary"], extra], ignore_index=True)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            (frame if not frame.empty else pd.DataFrame({"": ["Nothing this week"]})).to_excel(
                writer, sheet_name=name[:31], index=False)
    return buffer.getvalue()


__all__ = ["report_docx", "report_xlsx"]
