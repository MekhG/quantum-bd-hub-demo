"""Shared building blocks for the hub's look: chips, initials, channel tiles, the stage bar, cards, and one stylesheet.

Every page uses these instead of styling things itself, so the whole app stays consistent and a later restyle
(or the Phase 2 React app) only has to copy one set of rules.

Colour rule: custom HTML never hard-codes a text colour. Text inherits the theme's ink (dark in light mode, light in
dark mode) and tones come from translucent tints, so everything stays readable in both modes without knowing which
one is active.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence

import streamlit as st

BRAND = "#7A4A26"

# Tints are translucent so they sit well on white and on navy. Text on them is the theme's ink.
TONES = {
    "neutral": "color-mix(in srgb, currentColor 9%, transparent)",
    "blue": "rgba(139, 90, 43, 0.16)",
    "orange": "rgba(232, 89, 12, 0.16)",
    "green": "rgba(22, 163, 74, 0.16)",
    "yellow": "rgba(234, 179, 8, 0.22)",
    "purple": "rgba(124, 92, 230, 0.16)",
    "red": "rgba(220, 38, 38, 0.15)",
}
# Dot colours: decoration next to text (meaning is always in the words, never the colour alone).
DOTS = {"neutral": "currentColor", "blue": "#D29B65", "orange": "#E8590C", "green": "#16A34A",
        "yellow": "#CA8A04", "purple": "#7C5CE6", "red": "#DC2626"}

# Channel → (icon tint, icon stroke). Strokes are mid-tones that read on both backgrounds.
CHANNELS = {
    "email": ("rgba(139, 90, 43, 0.14)", "#D29B65"),
    "phone": ("rgba(20, 163, 168, 0.15)", "#139DA2"),
    "whatsapp": ("rgba(34, 163, 90, 0.15)", "#22A35A"),
    "meeting": ("rgba(124, 92, 230, 0.15)", "#8B6CE8"),
    "demo": ("rgba(124, 92, 230, 0.15)", "#8B6CE8"),
    "linkedin": ("rgba(139, 90, 43, 0.14)", "#D29B65"),
    "web": ("rgba(232, 89, 12, 0.14)", "#E8590C"),
    "other": ("rgba(232, 89, 12, 0.14)", "#E8590C"),
    "task": ("color-mix(in srgb, currentColor 9%, transparent)", "#6B7A8C"),
    "internal": ("color-mix(in srgb, currentColor 9%, transparent)", "#6B7A8C"),
}

# Lucide-style 24px stroke icons (MIT-licensed shapes), inlined so no icon font or network call is needed.
_ICON_PATHS = {
    "email": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 '
             '4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 '
             '2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/>',
    "whatsapp": '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22z"/>',
    "meeting": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
               '<path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "demo": '<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" '
            'height="12" rx="2"/>',
    "linkedin": '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 '
                '2v16"/>',
    "web": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 0 20M12 2a15.3 15.3 0 0 0 0 20"/>',
    "task": '<rect x="3" y="5" width="6" height="6" rx="1"/><path d="m3 17 2 2 4-4M13 6h8M13 12h8M13 18h8"/>',
    "internal": '<path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z"/><path d="M18 9h2a2 2 0 0 1 '
                '2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "forward": '<path d="m15 17 5-5-5-5"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/>',
}

# Initials colours for team members: white text on each passes 4.5:1.
_AVATAR_COLOURS = ["#7A4A26", "#6941C6", "#0B6E75", "#B54708", "#1E6B3A", "#9F1239"]

CSS = """
/* ---------- Type ---------- */
.qa-display { font-family: 'Fraunces', serif; font-weight: 600; letter-spacing: 0.005em; line-height: 1.15; }
.qa-muted { color: color-mix(in srgb, currentColor 74%, transparent); }
.qa-small { font-size: 0.86rem; }
.qa-mono { font-family: 'IBM Plex Mono', monospace; font-size: 0.9em; }
.qa-eyebrow { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: color-mix(in srgb, currentColor 74%, transparent); }

/* ---------- Chips ---------- */
.qa-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.qa-chip { display: inline-flex; align-items: center; gap: 6px; font-size: 0.78rem; font-weight: 500;
  line-height: 1.5; border-radius: 999px; padding: 1px 10px; white-space: nowrap;
  border: 1px solid color-mix(in srgb, currentColor 16%, transparent); }
.qa-chip .qa-dot { width: 7px; height: 7px; border-radius: 999px; flex: none; }
.qa-chip.qa-strong { font-weight: 600; }

/* ---------- Initials and tiles ---------- */
.qa-avatar { display: inline-flex; align-items: center; justify-content: center; border-radius: 12px;
  color: #FFFFFF; font-weight: 600; flex: none; }
.qa-tile { display: inline-flex; align-items: center; justify-content: center; border-radius: 10px; flex: none; }

/* ---------- Task / record rows ---------- */
.qa-row { display: flex; gap: 14px; align-items: flex-start; }
.qa-row-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.qa-row-title { font-weight: 600; font-size: 1rem; }
.qa-row-title .qa-sep { opacity: 0.6; padding: 0 4px; font-weight: 400; }

/* ---------- Stage bar ---------- */
.qa-stepper { list-style: none; margin: 4px 0 0; padding: 0; display: grid; gap: 6px;
  grid-template-columns: repeat(var(--qa-steps), minmax(0, 1fr)); }
.qa-stepper li { display: flex; flex-direction: column; gap: 7px; font-size: 0.76rem; min-width: 0; }
.qa-stepper { gap: 0; }
.qa-stepper li { gap: 10px; }
.qa-stepper .qa-bar { position: relative; height: 3px; margin-top: 7px;
  background: color-mix(in srgb, currentColor 14%, transparent); }
.qa-stepper .qa-bar::after { content: ""; position: absolute; left: 0; top: -6px; width: 15px; height: 15px;
  border-radius: 999px; box-sizing: border-box; background: var(--qa-card, #FFFFFF);
  border: 2px solid color-mix(in srgb, currentColor 28%, transparent); }
.qa-stepper .done .qa-bar { background: #A9713F; }
.qa-stepper .done .qa-bar::after { background: #7A4A26; border-color: #7A4A26; }
.qa-stepper .current .qa-bar::after { background: #E2B07E; border: 3px solid #7A4A26; }
.qa-stepper span { padding-right: 8px; }
.qa-stepper .current span { font-weight: 700; }
.qa-stepper .future span { color: color-mix(in srgb, currentColor 74%, transparent); }
.qa-stepper span { line-height: 1.25; overflow-wrap: anywhere; }
.qa-stepper-now { display: none; font-size: 0.82rem; margin-top: 6px; }
@media (max-width: 720px) { .qa-stepper span { display: none; } .qa-stepper-now { display: block; } }

/* ---------- Progress ---------- */
.qa-progress { height: 8px; border-radius: 999px; background: color-mix(in srgb, currentColor 12%, transparent);
  overflow: hidden; }
.qa-progress > div { height: 100%; background: linear-gradient(90deg, #A9713F, #7A4A26); border-radius: 999px; }

/* ---------- Definition lists (record details) ---------- */
.stHtml .qa-facts, .qa-facts { padding: 0 !important; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 8px 12px; margin: 0;
  font-size: 0.9rem; }
.qa-facts dt { margin: 0; padding: 0; color: color-mix(in srgb, currentColor 74%, transparent); }
.qa-facts dd { margin: 0; overflow-wrap: anywhere; }

/* ---------- Activity feed ---------- */
.qa-feed { list-style: none; margin: 0; padding: 0; }
.qa-feed li { display: flex; gap: 12px; padding: 12px 0;
  border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent); }
.qa-feed li:last-child { border-bottom: 0; }

/* ---------- Cards drawn by Streamlit containers (keyed so the selector is stable) ---------- */
/* Studio look: borderless white cards floating on the cream canvas, a colour edge for the channel. */
[class*="st-key-qa-card-"], [data-testid="stVerticalBlock"][class*="st-key-qa-card-"] {
  border-radius: 18px !important; border-color: transparent !important; background: var(--qa-card, #FFFFFF);
  box-shadow: 0 1px 2px rgba(43, 28, 16, 0.05), 0 8px 24px rgba(43, 28, 16, 0.07); }
[class*="st-key-qa-card-late-"] { box-shadow: inset 0 3px 0 #D9822B, 0 8px 24px rgba(43, 28, 16, 0.07); }
[class*="st-key-qa-card-info-"] { background: color-mix(in srgb, #8B5A2B 8%, var(--qa-card, #FFFFFF)); }
[class*="st-key-qa-card-"][class*="-chemail-"], [class*="st-key-qa-card-"][class*="-chlinkedin-"] { border-left: 5px solid #8B5A2B !important; }
[class*="st-key-qa-card-"][class*="-chphone-"] { border-left: 5px solid #139DA2 !important; }
[class*="st-key-qa-card-"][class*="-chwhatsapp-"] { border-left: 5px solid #22A35A !important; }
[class*="st-key-qa-card-"][class*="-chmeeting-"], [class*="st-key-qa-card-"][class*="-chdemo-"] { border-left: 5px solid #8B6CE8 !important; }
[class*="st-key-qa-card-"][class*="-chtask-"], [class*="st-key-qa-card-"][class*="-chinternal-"],
[class*="st-key-qa-card-"][class*="-chweb-"], [class*="st-key-qa-card-"][class*="-chother-"] { border-left: 5px solid #B8A691 !important; }
[class*="st-key-qa-topbar"] { padding: 0.55rem 0.7rem; margin-bottom: 0.2rem; border-radius: 999px;
  background: var(--qa-card, #FFFFFF); box-shadow: 0 1px 2px rgba(43, 28, 16, 0.05), 0 6px 18px rgba(43, 28, 16, 0.06); }
[class*="st-key-qa-attention"] { margin: 0.1rem 0 0.6rem; }
[class*="st-key-qa-attention"] a { border-radius: 999px; padding: 2px 12px !important;
  background: color-mix(in srgb, #D9822B 12%, transparent); border: 1px solid color-mix(in srgb, #D9822B 35%, transparent); }
.qa-eyebrow-ch { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
  color: color-mix(in srgb, currentColor 70%, transparent); }

/* Plain bordered boxes and metric tiles get the same floating white card look (keys come from _tag_boxes). */
[class*="st-key-qa-box-"], [data-testid="stMetric"] {
  border-radius: 18px !important; border-color: transparent !important; background: var(--qa-card, #FFFFFF);
  box-shadow: 0 1px 2px rgba(43, 28, 16, 0.05), 0 8px 24px rgba(43, 28, 16, 0.07); }
[data-testid="stMetric"] { padding: 14px 18px !important; }
[data-testid="stExpander"] details { border-radius: 16px; background: var(--qa-card, #FFFFFF); }

/* Page banner */
.qa-hero { display: flex; gap: 20px; align-items: center; padding: 22px 28px; margin: 0.2rem 0 0.8rem;
  border-radius: 24px; background: linear-gradient(115deg, color-mix(in srgb, #C9A27E 30%, transparent),
  color-mix(in srgb, #C9A27E 8%, transparent)); }
.qa-hero-badge { flex: none; width: 58px; height: 58px; border-radius: 999px; background: #7A4A26; color: #FBF7F1;
  display: inline-flex; align-items: center; justify-content: center; }
.qa-ms { font-family: 'Material Symbols Rounded'; font-size: 30px; line-height: 1; font-weight: 400;
  font-feature-settings: 'liga'; -webkit-font-feature-settings: 'liga'; letter-spacing: normal; }
.qa-hero-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.qa-hero-crumb { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
  color: color-mix(in srgb, currentColor 72%, transparent); }
.qa-hero .qa-hero-title, .stHtml h1.qa-hero-title { font-family: 'Fraunces', serif; font-weight: 600; font-size: 2.05rem;
  line-height: 1.12; margin: 0; padding: 0; }
.qa-hero-sub { margin: 4px 0 0; font-size: 0.95rem; color: color-mix(in srgb, currentColor 78%, transparent); }
@media (max-width: 640px) { .qa-hero { padding: 16px 18px; gap: 14px; } .qa-hero-badge { width: 44px; height: 44px; } }

/* Every Streamlit heading in Fraunces (the theme's headingFont alone doesn't reach st.title / st.subheader). */
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3,
[data-testid="stMarkdownContainer"] h1, [data-testid="stMarkdownContainer"] h2, [data-testid="stMarkdownContainer"] h3,
[data-testid="stMarkdownContainer"] h4 { font-family: 'Fraunces', Georgia, serif !important; font-weight: 600 !important; }

/* On a phone the command bar wraps onto two rows: a rounded card instead of a pill. */
@media (max-width: 640px) { [class*="st-key-qa-topbar"] { border-radius: 22px; } }

/* Title sizing: Fraunces headings get slightly tighter letter spacing. */
h1 { letter-spacing: 0.005em; }

/* Demo banner (portfolio build): a quiet beige strip above the top bar */
.qa-demo-banner { font-size: 0.82rem; padding: 6px 12px; margin: -0.6rem 0 0.4rem; border-radius: 8px;
  background: color-mix(in srgb, #C9A27E 22%, transparent); border: 1px solid color-mix(in srgb, #8B5A2B 30%, transparent); }

/* User card (in the person menu, top right) */
.qa-user { display: flex; align-items: center; gap: 10px; }
.qa-user b { display: block; font-weight: 600; }
"""


def _tag_boxes() -> None:
    """Streamlit gives a bordered st.container no attribute CSS can find, so every bordered container without a key
    gets one ('qa-box-<n>', counted afresh on each run so it is the same from run to run) and the stylesheet can turn
    it into a card. Installed once per process; containers with their own key are left alone."""
    from streamlit.elements.layouts import LayoutsMixin

    if getattr(LayoutsMixin, "_qa_tagged", False):
        return
    original = LayoutsMixin.container

    def container(self, *, border=None, key=None, **kwargs):
        if border and key is None:
            n = st.session_state.get("_qa_box_seq", 0) + 1
            st.session_state["_qa_box_seq"] = n
            key = f"qa-box-{n}"
        return original(self, border=border, key=key, **kwargs)

    LayoutsMixin.container = container
    LayoutsMixin._qa_tagged = True
    st.container = st._main.container  # st.container was bound before the patch


def inject_css() -> None:
    _tag_boxes()
    st.session_state["_qa_box_seq"] = 0  # a fresh count for this run
    # Cards are white on the cream page in light mode and a lifted espresso in dark mode.
    dark = getattr(getattr(st.context, "theme", None), "type", "light") == "dark"
    card_bg = "#261C15" if dark else "#FFFFFF"
    st.html(f"<style>:root {{ --qa-card: {card_bg}; }}{CSS}</style>")


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text))


def chip(text: str, tone: str = "neutral", *, dot: bool = False, strong: bool = False) -> str:
    dot_html = f'<span class="qa-dot" style="background:{DOTS.get(tone, DOTS["neutral"])}"></span>' if dot else ""
    cls = "qa-chip qa-strong" if strong else "qa-chip"
    return f'<span class="{cls}" style="background:{TONES.get(tone, TONES["neutral"])}">{dot_html}{esc(text)}</span>'


def chips(items: Iterable[str | tuple[str, str]]) -> str:
    """Items are text (neutral) or (text, tone); blanks are skipped."""
    parts = []
    for item in items:
        text, tone = (item, "neutral") if isinstance(item, str) else item
        if text:
            parts.append(chip(text, tone))
    return f'<div class="qa-chips">{"".join(parts)}</div>' if parts else ""


def initials(name: str) -> str:
    words = [w for w in (name or "").replace(".", " ").split() if w[:1].isalpha()
             and w.lower() not in {"dr", "prof", "mr", "ms", "mrs"}]
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def avatar(name: str, size: int = 34, *, colour: str | None = None) -> str:
    colour = colour or _AVATAR_COLOURS[sum(map(ord, name or "")) % len(_AVATAR_COLOURS)]
    return (f'<span class="qa-avatar" aria-hidden="true" style="width:{size}px;height:{size}px;'
            f'background:{colour};font-size:{max(11, size // 2.8):.0f}px">{esc(initials(name))}</span>')


def icon(name: str, size: int = 18, stroke: str = "#6B7A8C") -> str:
    """An inline icon as an <img> with an SVG data URI: Streamlit's HTML sanitiser strips raw <svg>, but keeps
    images. The stroke is fixed (an image can't inherit text colour), so callers pass a mid-tone that reads on
    both light and dark backgrounds."""
    import base64

    path = _ICON_PATHS.get(name, _ICON_PATHS["task"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
           f'stroke="{stroke}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{path}</svg>')
    data = base64.b64encode(svg.encode()).decode()
    return f'<img src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" alt="" style="display:block">'


def channel_tile(channel: str, size: int = 42) -> str:
    tint, stroke = CHANNELS.get(channel, CHANNELS["task"])
    return (f'<span class="qa-tile" aria-hidden="true" style="width:{size}px;height:{size}px;background:{tint}">'
            f'{icon(_ICON_ALIAS.get(channel, channel), int(size * 0.48), stroke)}</span>')


_ICON_ALIAS = {"other": "web"}


def stage_stepper(stages: Sequence[str], current: str | None) -> str:
    """The pipeline as a progress bar. Stages after the current one are 'future'; an off-path stage
    (Ghosted / Rejected) shows nothing as current and is named in the label instead."""
    index = list(stages).index(current) if current in stages else -1
    items = []
    for i, name in enumerate(stages):
        state = "done" if i < index else ("current" if i == index else "future")
        items.append(f'<li class="{state}"><div class="qa-bar"></div><span>{esc(name)}</span></li>')
    where = f"{current}, step {index + 1} of {len(stages)}" if index >= 0 else (current or "not started")
    # On a phone the per-step names are hidden and one line names where the lead is (aria-hidden: the list's
    # label already says it).
    now = (f'<div class="qa-stepper-now" aria-hidden="true">Stage {index + 1} of {len(stages)} · '
           f'<b>{esc(current)}</b></div>') if index >= 0 else ""
    return (f'<ol class="qa-stepper" style="--qa-steps:{len(stages)}" aria-label="Pipeline stage: {esc(where)}">'
            f'{"".join(items)}</ol>{now}')


def progress(fraction: float, label: str) -> str:
    pct = max(0.0, min(1.0, fraction)) * 100
    return (f'<div class="qa-progress" role="img" aria-label="{esc(label)}"><div style="width:{pct:.0f}%"></div>'
            f'</div>')


def facts(pairs: Iterable[tuple[str, str]]) -> str:
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs if v not in (None, ""))
    return f'<dl class="qa-facts">{rows}</dl>'


def day(value) -> str:
    """'6 Oct' (no leading zero) — easier to scan than '06 Oct'."""
    return f"{value.day} {value:%b}" if value is not None else ""


def html_block(markup: str) -> None:
    st.html(markup)


def card(key: str, tone: str = "plain"):
    """A card container with a stable CSS hook. tone: plain / late (amber top edge) / info (tinted). A key holding
    '-ch<channel>-' gets that channel's colour edge on the left."""
    return st.container(border=True, key=f"qa-card-{tone}-{key}")
