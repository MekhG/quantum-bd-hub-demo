"""GTM charts: a small text-outline language for flow diagrams, a layout engine, a renderer (matplotlib → PNG, SVG,
PDF), the automatic GTM map from the cadence configuration, templates (partner and reseller models), the
institute-category comparison, and saved diagrams.

The outline language (shown on the page as help):

    # Title                      first line, optional
    > Subtitle                   optional
    ## Section heading           starts a section
    A -> B -> C                  a flow: boxes joined by arrows (more lines = more rows in the section)
    Box [small text]             text in brackets is printed small inside the box
    Box {12 at this step}        text in braces is a badge above the box
    note: Goal for this flow     a caption under the section
    Parent:                      a parent box with branches; each indented "- " line is one branch
      - Child -> Next
    hub: Demo                    a hub with spokes; each indented "- " line is one spoke
      - Reseller [sells under NDA]

Everything is rule-based; no AI.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    AuditLog,
    CadenceStep,
    Diagram,
    Lead,
    Opportunity,
    Organisation,
    Track,
    User,
)
from app.services.cadence import IST, ist_date, today_ist

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
DIAGRAM_KINDS = ("custom", "gtm", "cadence", "partner", "reseller")

# Look: the demo's brown palette, so charts match the app.
EDGE, FILL, START_FILL, HUB_FILL, INK, MUTED = "#7A4A26", "#F7F0E6", "#EADCC6", "#8B5A2B", "#2B1C10", "#7A6555"
BADGE_FILL, BADGE_ALERT, BADGE_INK = "#EFE3CF", "#FDE6DA", "#2B1C10"

W = 11.0          # figure width, inches (landscape)
MARGIN = 0.5
GAP_X = 0.42      # between boxes in a row
ROW_GAP = 0.32
BOX_H, BOX_H_SUB = 0.5, 0.72


class DiagramError(ValueError):
    """Shown to the person as-is (with the line number when it's an outline problem)."""


def _require(user: User | None, role: str = "member") -> None:
    if user is None or _ROLE_RANK.get(user.role, -1) < _ROLE_RANK[role]:
        raise PermissionError(f"This needs {role} access.")


# ---------------------------------------------------------------- the outline language


@dataclass
class Node:
    label: str
    sub: str | None = None
    badge: str | None = None
    start: bool = False


@dataclass
class Section:
    title: str | None = None
    note: str | None = None
    kind: str = "flow"                 # flow · tree · hub
    rows: list[list[Node]] = field(default_factory=list)
    root: Node | None = None           # tree parent or hub centre


@dataclass
class Chart:
    title: str | None = None
    subtitle: str | None = None
    sections: list[Section] = field(default_factory=list)


_NODE_RE = re.compile(r"^(?P<label>[^\[\{]*?)\s*(?:\[(?P<sub>[^\]]*)\])?\s*(?:\{(?P<badge>[^}]*)\})?\s*$")


def parse_node(text: str, line_no: int) -> Node:
    match = _NODE_RE.match(text.strip())
    if not match or not match.group("label").strip():
        raise DiagramError(f"Line {line_no}: every box needs a name (got “{text.strip()}”).")
    return Node(match.group("label").strip()[:80], (match.group("sub") or "").strip()[:120] or None,
                (match.group("badge") or "").strip()[:60] or None)


def parse_chain(text: str, line_no: int) -> list[Node]:
    parts = [p for p in re.split(r"\s*(?:->|→)\s*", text.strip())]
    if any(not p.strip() for p in parts):
        raise DiagramError(f"Line {line_no}: an arrow is missing a box on one side.")
    nodes = [parse_node(p, line_no) for p in parts]
    if len(nodes) > 12:
        raise DiagramError(f"Line {line_no}: at most 12 boxes in one row — split it over two lines.")
    return nodes


def parse_outline(text: str) -> Chart:
    chart = Chart()
    section: Section | None = None
    pending_parent: tuple[Section, str] | None = None   # waiting for indented lines

    def current() -> Section:
        nonlocal section
        if section is None:
            section = Section()
            chart.sections.append(section)
        return section

    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("//"):
            continue
        stripped = raw.strip()
        indented = raw[:1] in (" ", "\t") and stripped.startswith(("-", "•", "*"))
        if indented:
            if pending_parent is None:
                raise DiagramError(f"Line {line_no}: an indented “- …” line must follow a “Parent:” or “hub:” line.")
            sec, kind = pending_parent
            body = stripped.lstrip("-•* ").strip()
            if kind == "hub":
                sec.rows.append([parse_node(body.replace("|", "[", 1) + "]" if "|" in body and "[" not in body
                                            else body, line_no)])
            else:
                sec.rows.append(parse_chain(body, line_no))
            continue
        pending_parent = None
        if stripped.startswith("## "):
            section = Section(title=stripped[3:].strip()[:120])
            chart.sections.append(section)
        elif stripped.startswith("# "):
            chart.title = stripped[2:].strip()[:120]
        elif stripped.startswith(">"):
            chart.subtitle = stripped[1:].strip()[:160]
        elif stripped.lower().startswith("note:"):
            current().note = stripped[5:].strip()[:200]
        elif stripped.lower().startswith("hub:"):
            sec = Section(title=section.title if section is not None and not section.rows and section.root is None
                          else None, kind="hub")
            if section is not None and not section.rows and section.root is None:
                chart.sections.remove(section)
            sec.root = parse_node(stripped[4:], line_no)
            chart.sections.append(sec)
            section = sec
            pending_parent = (sec, "hub")
        elif stripped.endswith(":") and "->" not in stripped and "→" not in stripped:
            sec = current()
            if sec.rows or sec.root is not None:
                sec = Section()
                chart.sections.append(sec)
                section = sec
            sec.kind, sec.root = "tree", parse_node(stripped[:-1], line_no)
            sec.root.start = True
            pending_parent = (sec, "tree")
        else:
            sec = current()
            if sec.kind != "flow":
                sec = Section()
                chart.sections.append(sec)
                section = sec
            chain = parse_chain(stripped, line_no)
            chain[0].start = True
            sec.rows.append(chain)
    chart.sections = [s for s in chart.sections if s.rows or s.root is not None]
    if not chart.sections:
        raise DiagramError("Nothing to draw yet — type a flow such as “Email -> Call -> Demo”.")
    for sec in chart.sections:
        if sec.kind in ("tree", "hub") and not sec.rows:
            raise DiagramError(f"“{sec.root.label}” needs at least one indented “- …” line under it.")
    return chart


# ---------------------------------------------------------------- layout


def _text_width(text: str | None, size: float) -> float:
    """Rough width in inches of text in DejaVu Sans at `size` points (good enough for box sizing)."""
    return len(text or "") * size * 0.0083


def _wrap(text: str | None, width_chars: int) -> str | None:
    if not text:
        return text
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width_chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    return "\n".join(lines[:3])


@dataclass
class Box:
    node: Node
    x: float
    y: float          # top
    w: float
    h: float
    scale: float = 1.0


@dataclass
class Link:
    a: Box
    b: Box
    style: str = "straight"   # straight · elbow · radial


@dataclass
class Layout:
    height: float
    boxes: list[Box] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    texts: list[tuple[float, float, str, dict]] = field(default_factory=list)


def _label(node: Node) -> str:
    return _wrap(node.label, 24) or node.label


def _box_size(node: Node, scale: float) -> tuple[float, float]:
    label = _label(node)
    label_w = max(_text_width(line, 9.5 * scale) for line in label.splitlines()) * (1.15 if node.start else 1.0)
    sub = _wrap(node.sub, 34)
    sub_w = max((_text_width(line, 7.5 * scale) for line in (sub or "").splitlines()), default=0)
    w = min(max(label_w, sub_w) + 0.36 * scale, 3.0 * scale)
    w = max(w, 1.15 * scale)
    lines = len((sub or "").splitlines()) + len(label.splitlines()) - 1
    h = (BOX_H if not sub else BOX_H_SUB) + 0.14 * max(lines - (1 if sub else 0), 0)
    return w, h * max(scale, 0.8)


def _row(nodes: list[Node], x0: float, y: float, avail: float) -> tuple[list[Box], float]:
    sizes = [_box_size(n, 1.0) for n in nodes]
    needed = sum(w for w, _ in sizes) + GAP_X * (len(nodes) - 1)
    scale = min(1.0, avail / needed) if needed else 1.0
    if scale < 1.0:
        sizes = [_box_size(n, scale) for n in nodes]
        needed = sum(w for w, _ in sizes) + GAP_X * scale * (len(nodes) - 1)
        if needed > avail:  # minimum widths still too wide: squeeze evenly, text included
            factor = avail / needed
            scale *= factor
            sizes = [(w * factor, h) for w, h in sizes]
    height = max(h for _, h in sizes)
    boxes, x = [], x0
    gap = (avail - sum(w for w, _ in sizes)) / (len(nodes) - 1) if len(nodes) > 1 and scale < 1.0 else GAP_X
    for node, (w, h) in zip(nodes, sizes):
        boxes.append(Box(node, x, y + (height - h) / 2, w, h, scale))
        x += w + gap
    return boxes, height


def layout(chart: Chart) -> Layout:
    out = Layout(height=0)
    y = MARGIN
    if chart.title:
        out.texts.append((W / 2, y + 0.18, chart.title, {"size": 18, "weight": "bold", "ha": "center"}))
        y += 0.42
    if chart.subtitle:
        out.texts.append((W / 2, y + 0.08, chart.subtitle, {"size": 10, "style": "italic", "ha": "center",
                                                            "color": MUTED}))
        y += 0.3
    if chart.title or chart.subtitle:
        y += 0.15
    for index, sec in enumerate(chart.sections, start=1):
        if sec.title:
            out.texts.append((MARGIN, y + 0.12, sec.title, {"size": 12, "weight": "bold", "ha": "left",
                                                            "color": EDGE}))
            y += 0.38
        avail = W - 2 * MARGIN
        badge_room = 0.22 if any(n.badge for row in sec.rows for n in row) or (sec.root and sec.root.badge) else 0
        y += badge_room
        if sec.kind == "flow":
            for nodes in sec.rows:
                indent = 0.9 if len(nodes) <= 4 else 0.0     # short flows sit a little in, like the team's chart
                boxes, h = _row(nodes, MARGIN + indent, y, avail - indent)
                out.boxes += boxes
                out.links += [Link(a, b) for a, b in zip(boxes, boxes[1:])]
                y += h + ROW_GAP + badge_room
        elif sec.kind == "tree":
            root_w, root_h = _box_size(sec.root, 1.0)
            child_x = MARGIN + root_w + 0.6
            rows_top = y
            first_boxes = []
            for nodes in sec.rows:
                boxes, h = _row(nodes, child_x, y, W - MARGIN - child_x)
                out.boxes += boxes
                out.links += [Link(a, b) for a, b in zip(boxes, boxes[1:])]
                first_boxes.append(boxes[0])
                y += h + ROW_GAP + badge_room
            block = y - ROW_GAP - badge_room - rows_top
            root = Box(sec.root, MARGIN, rows_top + (block - root_h) / 2, root_w, root_h)
            out.boxes.append(root)
            out.links += [Link(root, b, "elbow") for b in first_boxes]
        else:  # hub
            spokes = [row[0] for row in sec.rows][:10]
            centre_w, centre_h = _box_size(sec.root, 1.15)
            radius_y = 1.15 + 0.08 * len(spokes)
            radius_x = 3.6
            cx, cy = W / 2, y + radius_y + 0.45
            centre = Box(sec.root, cx - centre_w / 2, cy - centre_h / 2, centre_w, centre_h, 1.15)
            out.boxes.append(centre)
            import math

            for i, node in enumerate(spokes):
                angle = -math.pi / 2 + 2 * math.pi * i / len(spokes)
                w, h = _box_size(node, 1.0)
                bx, by = cx + radius_x * math.cos(angle), cy + radius_y * math.sin(angle)
                box = Box(node, bx - w / 2, by - h / 2, w, h)
                out.boxes.append(box)
                out.links.append(Link(centre, box, "radial"))
            y = cy + radius_y + 0.5
        if sec.note:
            out.texts.append((W / 2, y - 0.06, sec.note, {"size": 8.5, "style": "italic", "ha": "center",
                                                          "color": MUTED}))
            y += 0.22
        if index < len(chart.sections):
            y += 0.28
    out.height = y + MARGIN - 0.2
    return out


# ---------------------------------------------------------------- drawing


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def draw(chart: Chart):
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    plt = _plt()
    lay = layout(chart)
    fig = plt.figure(figsize=(W, max(lay.height, 1.5)))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(max(lay.height, 1.5), 0)
    ax.axis("off")
    fig.patch.set_facecolor("white")
    for x, y, text, style in lay.texts:
        ax.text(x, y, text, va="center", family="DejaVu Sans", color=style.pop("color", INK), fontsize=style.pop("size"),
                **style)
    for link in lay.links:
        a, b = link.a, link.b
        if link.style == "straight":
            start, end = (a.x + a.w, a.y + a.h / 2), (b.x, b.y + b.h / 2)
            conn = "arc3"
        elif link.style == "elbow":
            # Parent → branch: across, down (or up) to the branch's row, then an arrow into the first box.
            mid_x = a.x + a.w + 0.28
            ay, by = a.y + a.h / 2, b.y + b.h / 2
            ax.plot([a.x + a.w, mid_x, mid_x], [ay, ay, by], color=EDGE, linewidth=1.6, zorder=1,
                    solid_joinstyle="round")
            start, end = (mid_x, by), (b.x, by)
            conn = "arc3"
        else:
            ax_c, ay_c = a.x + a.w / 2, a.y + a.h / 2
            bx_c, by_c = b.x + b.w / 2, b.y + b.h / 2
            start, end = (ax_c, ay_c), (bx_c, by_c)
            conn = "arc3"
        # Spokes are relationships, not steps: plain lines (they run behind the boxes).
        arrow = FancyArrowPatch(start, end, arrowstyle="-" if link.style == "radial" else "-|>", mutation_scale=14,
                                linewidth=1.6, color=EDGE,
                                connectionstyle=conn, shrinkA=0 if link.style in ("elbow", "radial") else 2,
                                shrinkB=0 if link.style == "radial" else 2, zorder=1)
        ax.add_patch(arrow)
    for box in lay.boxes:
        node = box.node
        hub = box.scale > 1.1
        fill = HUB_FILL if hub else (START_FILL if node.start else FILL)
        ax.add_patch(FancyBboxPatch((box.x, box.y), box.w, box.h, boxstyle="round,pad=0,rounding_size=0.08",
                                    facecolor=fill, edgecolor=EDGE, linewidth=1.4, zorder=2))
        ink = "white" if hub else INK
        sub = _wrap(node.sub, 34)
        label = _label(node)
        size = 9.5 * min(box.scale, 1.15)
        weight = "bold" if node.start or hub else "normal"
        if sub:
            label_lines, sub_lines = len(label.splitlines()), len(sub.splitlines())
            label_h, sub_h = 0.16 * label_lines, 0.125 * sub_lines
            top = box.y + (box.h - label_h - sub_h - 0.05) / 2
            ax.text(box.x + box.w / 2, top + label_h / 2, label, ha="center", va="center", fontsize=size,
                    weight=weight, color=ink, zorder=3, linespacing=1.1)
            ax.text(box.x + box.w / 2, top + label_h + 0.05 + sub_h / 2, sub, ha="center", va="center",
                    fontsize=7.5 * min(box.scale, 1.0), color=ink if hub else MUTED, zorder=3, linespacing=1.15)
        else:
            ax.text(box.x + box.w / 2, box.y + box.h / 2, label, ha="center", va="center", fontsize=size,
                    weight=weight, color=ink, zorder=3, linespacing=1.1)
        if node.badge:
            alert = bool(re.search(r"[1-9]\d*\s+overdue", node.badge))
            bw = _text_width(node.badge, 7) + 0.2
            ax.add_patch(FancyBboxPatch((box.x + box.w - bw, box.y - 0.24), bw, 0.19,
                                        boxstyle="round,pad=0,rounding_size=0.09",
                                        facecolor=BADGE_ALERT if alert else BADGE_FILL, edgecolor="none", zorder=4))
            ax.text(box.x + box.w - bw / 2, box.y - 0.145, node.badge, ha="center", va="center", fontsize=7,
                    color=BADGE_INK, zorder=5)
    return fig


def render(chart: Chart, fmt: str = "png") -> bytes:
    if fmt not in ("png", "svg", "pdf"):
        raise DiagramError("Download as PNG, SVG or PDF.")
    fig = draw(chart)
    buffer = io.BytesIO()
    fig.savefig(buffer, format=fmt, dpi=200 if fmt == "png" else None, facecolor="white")
    _plt().close(fig)
    return buffer.getvalue()


def describe(chart: Chart) -> str:
    """Plain-text description of a diagram (the image's alt text for screen readers)."""
    parts = [chart.title or "Diagram"]
    for sec in chart.sections:
        rows = [" → ".join(n.label for n in row) for row in sec.rows]
        if sec.kind == "tree":
            rows = [f"{sec.root.label} branches to: " + "; ".join(rows)]
        elif sec.kind == "hub":
            rows = [f"{sec.root.label} at the centre, linked to: " + ", ".join(r[0].label for r in sec.rows)]
        parts.append((f"{sec.title}: " if sec.title else "") + "; ".join(rows))
    return ". ".join(parts)[:1500]


def render_outline(text: str, fmt: str = "png") -> bytes:
    return render(parse_outline(text), fmt)


# ---------------------------------------------------------------- the automatic GTM map


def _channels(step: CadenceStep) -> str:
    names = []
    for kind in step.activity_types:
        name = {"Phone call": "Call", "WhatsApp message": "WhatsApp", "WhatsApp call": "WhatsApp call",
                "LinkedIn message": "LinkedIn"}.get(kind.name, kind.name)
        names.append(name)
    return (" / " if step.complete_on_any else " + ").join(dict.fromkeys(names)) or "Task"


def _days(step: CadenceStep, first: bool) -> str:
    if first and not step.days_after_previous:
        return "day 0"
    low, high = step.days_after_previous, step.days_after_previous + step.grace_days
    return f"+{low} days" if low == high else f"+{low}–{high} days"


def step_counts(session: Session, today: date | None = None) -> dict[int, tuple[int, int]]:
    """Per cadence step: (leads currently on it, leads whose task for it is overdue)."""
    today = today or today_ist()
    at_step: dict[tuple[int, int], int] = {}
    for track_id, step_no, n in session.execute(
            select(Lead.track_id, Lead.current_step_no, func.count(Lead.id))
            .where(Lead.cadence_status.in_(("active", "awaiting_reply")), Lead.current_step_no.is_not(None))
            .group_by(Lead.track_id, Lead.current_step_no)):
        at_step[(track_id, step_no)] = int(n)
    overdue: dict[int, set[int]] = {}
    for step_id, grace, scheduled, lead_id in session.execute(
            select(CadenceStep.id, CadenceStep.grace_days, Activity.scheduled_at, Activity.lead_id)
            .join(Activity, Activity.cadence_step_id == CadenceStep.id).where(Activity.status == "planned")):
        due = ist_date(scheduled)
        if due and due + timedelta(days=grace or 0) < today:
            overdue.setdefault(step_id, set()).add(lead_id)
    out = {}
    for step in session.scalars(select(CadenceStep)):
        out[step.id] = (at_step.get((step.track_id, step.step_no), 0), len(overdue.get(step.id, set())))
    return out


def _clean(text: str) -> str:
    # Outline control characters can't appear inside box text.
    return re.sub(r"[\[\]{}]|->|→", " ", text or "").replace(":", " ").strip()


def gtm_outline(session: Session, track_ids: list[int] | None = None, *, live: bool = True,
                today: date | None = None) -> str:
    """The GTM map, drawn from the tracks and cadences in Configuration."""
    today = today or today_ist()
    query = select(Track).where(Track.is_active.is_(True)).order_by(Track.sort_order)
    if track_ids:
        query = query.where(Track.id.in_(track_ids))
    counts = step_counts(session, today) if live else {}
    lines = ["# GTM map",
             "> Go-to-market outreach flow — from the hub's cadence settings" +
             (f" · live counts on {today:%d %b %Y}" if live else "")]
    for number, track in enumerate(session.scalars(query), start=1):
        steps = list(track.cadence_steps)
        if not steps:
            continue
        lines.append(f"## {number}. {_clean(track.name)}")
        chain = []
        for i, step in enumerate(steps):
            text = f"{_clean(step.name)} [{_channels(step)} · {_days(step, i == 0)}]"
            if live:
                n, late = counts.get(step.id, (0, 0))
                if n or late:
                    text += " {" + f"{n} at this step" + (f" · {late} overdue" if late else "") + "}"
            chain.append(text)
        tiers = [t for t in (track.sub_tiers or []) if t]
        if tiers:
            lines.append(f"{_clean(track.name)}:")
            for tier in tiers:
                lines.append(f"  - {_clean(tier)} -> " + " -> ".join(chain))
        else:
            lines.append(" -> ".join([f"{_clean(track.name)}"] + chain))
        if track.goal:
            lines.append(f"note: Goal — {_clean(track.goal)[:150]}")
    if len(lines) == 2:
        raise DiagramError("No active track has cadence steps yet.")
    return "\n".join(lines)


def cadence_outline(session: Session, track_id: int) -> str:
    track = session.get(Track, track_id)
    if track is None:
        raise DiagramError("Pick a track.")
    text = gtm_outline(session, [track_id], live=False)
    body = [line for line in text.splitlines() if not line.startswith(("# ", ">"))]
    return "\n".join([f"# {_clean(track.name)} — cadence"] + body)


TEMPLATES: dict[str, str] = {
    "partner": """# Partner models
> How Demo works with partners (a fictional example)
hub: Demo [simulators · kits · support]
  - Reseller [sells in its region from a partner price list]
  - Referral partner [introduces organisations · referral fee]
  - Training partner [runs workshops on our platforms]
  - EdTech platform [lists our simulators in its catalogue]""",
    "reseller": """# Reseller model
> Who does what in a reseller sale (a fictional example)
## The sale
Demo [partner price list] -> Reseller [quotes and invoices the organisation] -> Organisation [issues the purchase order]
note: Deal registration shows any overlap before a reseller starts.
## After the order
Delivery by Demo -> Installation and training -> Warranty and support
## Growing the partnership
Reseller -> Steady sales -> Distribution partner""",
    "blank": """# My diagram
## First flow
Start -> Step two [details in brackets] -> Done
note: A caption under the flow""",
}


# ---------------------------------------------------------------- segments compared (industry, public/private…)


# Fixed colour per category (validated palette; "Not set" stays grey), so a filter never repaints a category.
CATEGORY_COLOURS = {"Public · Urban": "#8B5A2B", "Public · Rural": "#2F7F7A", "Private · Urban": "#16A34A",
                    "Private · Rural": "#7C5CE6", "Public": "#8B5A2B", "Private": "#16A34A", "Urban": "#8B5A2B",
                    "Rural": "#2F7F7A"}
SEGMENT_METRICS = ("Leads", "Contacted", "Replied", "Demos", "Quotes sent", "Won", "Won value (₹)", "Win rate")
GROUPINGS = {"Industry": "industry", "Public / private × urban / rural": "both", "Public / private": "sector",
             "Urban / rural": "area"}
TOP_INDUSTRIES = 6  # the rest are added up as "Other industries", so the charts stay readable


def segment_comparison(session: Session, start: date, end: date, grouping: str = "both") -> pd.DataFrame:
    """Segments compared over [start, end] — by industry, or education's public/private and urban/rural: leads
    (all, now), and what happened in the period."""
    from app.services.report_metrics import _demos, _replies, _quotes_sent, area_of, sector_of, touches

    leads = pd.DataFrame(session.execute(
        select(Lead.id, Organisation.management, Organisation.area, Organisation.industry, Organisation.org_type)
        .join(Organisation, Organisation.id == Lead.org_id)).all(),
        columns=["lead_id", "management", "area", "industry", "org_type"])
    if leads.empty:
        return pd.DataFrame(columns=["Category", *SEGMENT_METRICS])
    industry = leads["industry"].mask(leads["industry"].isna() & (leads["org_type"] == "individual"), "Individuals")
    industry = industry.fillna("Not set")
    biggest = list(industry[industry != "Not set"].value_counts().index[:TOP_INDUSTRIES])

    def name(m, a, i):
        if grouping == "industry":
            return i if i in biggest or i in ("Not set", "Individuals") else "Other industries"
        if grouping == "sector":
            return sector_of(m)
        if grouping == "area":
            return area_of(a)
        sector, area = sector_of(m), area_of(a)
        return "Not set" if sector == area == "Not set" else f"{sector} · {area}"

    leads["Category"] = [name(m, a, i) for m, a, i in zip(leads["management"], leads["area"], industry)]
    category = leads.set_index("lead_id")["Category"]
    s_dt = datetime.combine(start, time(0), tzinfo=IST)
    e_dt = datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST)
    contacted = touches(session, s_dt, e_dt)
    replies, demos = _replies(session, s_dt, e_dt), _demos(session, s_dt, e_dt)
    quotes = _quotes_sent(session, start, end)
    won = pd.DataFrame(session.execute(select(Opportunity.lead_id, Opportunity.value_inr).where(
        Opportunity.status == "won", Opportunity.po_date >= start, Opportunity.po_date <= end)).all(),
        columns=["lead_id", "value"])

    def distinct(frame: pd.DataFrame) -> pd.Series:
        if frame.empty:
            return pd.Series(dtype=int)
        frame = frame[frame["lead_id"].notna()]
        return frame.assign(c=frame["lead_id"].map(category)).groupby("c")["lead_id"].nunique()

    def counted(frame: pd.DataFrame) -> pd.Series:
        if frame.empty:
            return pd.Series(dtype=int)
        return frame["lead_id"].map(category).value_counts()

    won_value = (won.assign(c=won["lead_id"].map(category), v=won["value"].astype(float).fillna(0))
                 .groupby("c")["v"].sum()) if not won.empty else pd.Series(dtype=float)
    order = {"both": ["Public · Urban", "Public · Rural", "Private · Urban", "Private · Rural"],
             "sector": ["Public", "Private"], "area": ["Urban", "Rural"],
             "industry": [*biggest, "Other industries"]}[grouping]
    names = order + sorted(c for c in leads["Category"].unique() if c not in order)
    rows = []
    for c in names:
        n = int((leads["Category"] == c).sum())
        if not n:
            continue
        won_n = int(counted(won).get(c, 0))
        rows.append({"Category": c, "Leads": n, "Contacted": int(distinct(contacted).get(c, 0)),
                     "Replied": int(distinct(replies).get(c, 0)), "Demos": int(distinct(demos).get(c, 0)),
                     "Quotes sent": int(counted(quotes).get(c, 0)), "Won": won_n,
                     "Won value (₹)": float(won_value.get(c, 0.0)), "Win rate": won_n / n if n else 0.0})
    return pd.DataFrame(rows, columns=["Category", *SEGMENT_METRICS])


def comparison_chart(frame: pd.DataFrame, metrics: list[str], title: str, fmt: str = "png") -> bytes:
    """One small bar chart per metric (each with its own scale — never two scales on one axis)."""
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    from app.services.money import inr

    plt = _plt()
    metrics = [m for m in metrics if m in frame.columns] or ["Leads"]
    cols = 2 if len(metrics) > 1 else 1
    rows = (len(metrics) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(W, 2.5 * rows + 0.7), squeeze=False)
    fig.patch.set_facecolor("white")
    fig.suptitle(title, fontsize=14, weight="bold", color=INK, x=0.05, ha="left")
    categories = list(frame["Category"])
    bar_colours = [CATEGORY_COLOURS.get(c, "#B8A691") for c in categories]  # same colour per category everywhere
    for i, metric in enumerate(metrics):
        ax = axes[i // cols][i % cols]
        values = list(frame[metric]) if not frame.empty else []
        bars = ax.bar(categories, values, color=bar_colours, width=0.6)
        for bar, value in zip(bars, values):
            label = (inr(value, decimals=False) if metric == "Won value (₹)" else
                     f"{value:.0%}" if metric == "Win rate" else f"{int(value):,}")
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), label, ha="center", va="bottom", fontsize=8,
                    color=INK)
        ax.set_title(metric, fontsize=10, loc="left", color=INK)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="x", labelsize=8, length=0)
        if len(categories) > 4:  # industry names are long: slant them so they don't overlap
            ax.tick_params(axis="x", labelrotation=30)
            for tick in ax.get_xticklabels():
                tick.set_horizontalalignment("right")
        ax.tick_params(axis="y", labelsize=7, colors=MUTED)
        ax.grid(axis="y", color="#EDE3D6", linewidth=0.8)
        ax.set_axisbelow(True)
        if metric == "Win rate":
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
        elif metric == "Won value (₹)":
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"₹{v / 100000:.0f}L" if v else "0"))
        else:
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        top = max(values) if values else 0
        ax.set_ylim(0, top * 1.2 if top else 1)
        if not top:
            ax.set_yticks([])  # nothing yet: no scale to read
    for j in range(len(metrics), rows * cols):
        axes[j // cols][j % cols].axis("off")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    buffer = io.BytesIO()
    fig.savefig(buffer, format=fmt, dpi=200 if fmt == "png" else None, facecolor="white")
    plt.close(fig)
    return buffer.getvalue()


def period_range(label: str, today: date | None = None) -> tuple[date, date]:
    today = today or today_ist()
    fy = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    if label == "This FY":
        return fy, today
    if label == "Last FY":
        return date(fy.year - 1, 4, 1), fy - timedelta(days=1)
    if label == "This quarter":
        month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, month, 1), today
    if label == "Last 90 days":
        return today - timedelta(days=89), today
    return date(2000, 1, 1), today


# ---------------------------------------------------------------- saved diagrams


def diagrams_frame(session: Session) -> pd.DataFrame:
    rows = session.execute(select(Diagram, User.name).outerjoin(User, User.id == Diagram.updated_by_user_id)
                           .order_by(Diagram.updated_at.desc())).all()
    return pd.DataFrame([{"id": d.id, "Title": d.title, "Template": d.kind.title(), "Updated": d.updated_at,
                          "By": who or ""} for d, who in rows], columns=["id", "Title", "Template", "Updated", "By"])


def save_diagram(session: Session, *, title: str, outline: str, user: User, kind: str = "custom",
                 diagram_id: int | None = None, description: str | None = None) -> Diagram:
    _require(user)
    title = (title or "").strip()
    if not title:
        raise DiagramError("Give the diagram a title.")
    parse_outline(outline)  # refuse to save something that can't be drawn
    if kind not in DIAGRAM_KINDS:
        kind = "custom"
    diagram = session.get(Diagram, diagram_id) if diagram_id else None
    if diagram_id and diagram is None:
        raise DiagramError("That diagram was deleted — save it as a new one.")
    if diagram is None:
        diagram = Diagram(created_by_user_id=user.id, kind=kind)
        session.add(diagram)
    diagram.title, diagram.outline, diagram.updated_by_user_id = title[:200], outline, user.id
    diagram.description = (description or "").strip() or None
    session.flush()
    session.add(AuditLog(user_id=user.id, entity="diagrams", entity_id=str(diagram.id),
                         action="update" if diagram_id else "create", diff={"Diagram": [None, diagram.title]}))
    return diagram


def delete_diagram(session: Session, diagram_id: int, user: User) -> str:
    _require(user)
    diagram = session.get(Diagram, diagram_id)
    if diagram is None:
        raise DiagramError("That diagram was already deleted.")
    if user.role != "admin" and diagram.created_by_user_id != user.id:
        raise PermissionError("Only the person who made this diagram, or an admin, can delete it.")
    title = diagram.title
    session.delete(diagram)
    session.add(AuditLog(user_id=user.id, entity="diagrams", entity_id=str(diagram_id), action="delete",
                         diff={"Diagram": [title, None]}))
    session.flush()
    return title


__all__ = ["DiagramError", "describe", "GROUPINGS", "SEGMENT_METRICS", "TEMPLATES", "cadence_outline", "comparison_chart",
           "delete_diagram", "diagrams_frame", "gtm_outline", "parse_outline", "period_range", "render",
           "render_outline", "save_diagram", "segment_comparison"]
