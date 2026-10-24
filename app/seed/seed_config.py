"""Fill the configuration tables with Demo's BD setup (a fictional company — every value here is made up).

Safe to run again: each list is only seeded into an empty table (tracks are
matched by their fixed code), so renames and edits made later are never undone.

Run with:  python -m app.seed.seed_config
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ActivityType,
    AppSetting,
    CadenceStep,
    LostReason,
    NewsSource,
    Objection,
    OptionList,
    ProcurementRule,
    Product,
    SignalType,
    Stage,
    TaskAssignment,
    Track,
    User,
)


@dataclass(frozen=True)
class StepSpec:
    name: str
    days_after_previous: int
    grace_days: int
    activity_types: tuple[str, ...]
    complete_on_any: bool = False


@dataclass(frozen=True)
class TrackSpec:
    code: str
    name: str
    goal: str | None
    sender_mailbox: str | None
    sub_tiers: list[str] | None = None
    notes: str | None = None
    steps: tuple[StepSpec, ...] = field(default_factory=tuple)


# Demo's go-to-market: a fictional set of tracks and cadences, made up for this portfolio demo.
# days_after_previous = gap after the previous step; grace_days = how long before a planned task counts as overdue.
TRACKS: tuple[TrackSpec, ...] = (
    TrackSpec(
        code="cold_colleges",
        name="Colleges — cold outreach",
        goal="Book a first online demo with the department",
        sender_mailbox="hello@demo.example",
        steps=(
            StepSpec("Intro", 0, 1, ("Email", "LinkedIn message")),
            StepSpec("Call", 4, 2, ("Phone call",)),
            StepSpec("Case study email", 5, 2, ("Email",)),
            StepSpec("Break-up email", 7, 3, ("Email",)),
        ),
    ),
    TrackSpec(
        code="referrals",
        name="Referrals and past contacts",
        goal="Turn an introduction into a scoped proposal",
        sender_mailbox="riya@demo.example",
        steps=(
            StepSpec("Intro call", 0, 1, ("Phone call",)),
            StepSpec("Recap email", 1, 1, ("Email",)),
            StepSpec("Demo invite", 5, 2, ("Email", "WhatsApp message"), complete_on_any=True),
            StepSpec("Check-in call", 7, 2, ("Phone call",)),
        ),
    ),
    TrackSpec(
        code="research_labs",
        name="Research institutes",
        goal="Find labs that want joint workshops or a shared teaching lab",
        sender_mailbox="partners@demo.example",
        steps=(
            StepSpec("Email", 0, 1, ("Email",)),
            StepSpec("Follow-up email", 6, 2, ("Email",)),
            StepSpec("Call", 8, 2, ("Phone call",)),
        ),
    ),
    TrackSpec(
        code="edtech",
        name="EdTech platforms",
        goal="List our simulators in their course catalogue",
        sender_mailbox="partners@demo.example",
        steps=(
            StepSpec("LinkedIn message", 0, 1, ("LinkedIn message",)),
            StepSpec("Email", 4, 2, ("Email",)),
            StepSpec("Meeting request", 7, 2, ("Email", "Phone call"), complete_on_any=True),
        ),
    ),
    TrackSpec(
        code="schools",
        name="Schools — STEM labs",
        goal="Pilot a simulator club for senior classes",
        sender_mailbox="hello@demo.example",
        sub_tiers=["Senior secondary", "Secondary"],
        steps=(
            StepSpec("Email", 0, 1, ("Email",)),
            StepSpec("Call", 5, 2, ("Phone call",)),
            StepSpec("WhatsApp nudge", 7, 2, ("WhatsApp message",)),
        ),
    ),
    TrackSpec(
        code="inbound_queries",
        name="Website enquiries",
        goal="Answer the same day, then qualify and move the lead to the right track",
        sender_mailbox="hello@demo.example",
        notes="Enquiries arrive at hello@demo.example. The reply is due the day it arrives.",
        steps=(
            StepSpec("Reply", 0, 1, ("Email",)),
            StepSpec("Call", 2, 1, ("Phone call",)),
            StepSpec("Follow-up email", 5, 2, ("Email",)),
            StepSpec("Last nudge", 7, 2, ("WhatsApp message", "Phone call"), complete_on_any=True),
        ),
    ),
    TrackSpec(
        code="individuals",
        name="Individual learners (QSim Mini, NetSim Mini)",
        goal="Convert enquiries into annual licences",
        sender_mailbox="hello@demo.example",
        steps=(
            StepSpec("Welcome email", 0, 1, ("Email",)),
            StepSpec("WhatsApp check-in", 3, 1, ("WhatsApp message",)),
            StepSpec("Offer email", 7, 2, ("Email",)),
        ),
    ),
)

# Stages use the team's own status words, in pipeline order.
# (name, outcome, other wordings accepted on import).
STAGES: tuple[tuple[str, str, list[str]], ...] = (
    ("New", "open", ["verified", "fresh", "not contacted"]),
    ("First Contact Made", "open", ["first contact", "contacted", "email sent", "mail sent"]),
    ("Replied to Queries", "open", ["replied", "queries replied", "query replied"]),
    ("Sent Brochure", "open", ["brochure sent"]),
    ("Online Demonstration", "open", ["online demo", "online demo done", "virtual demo"]),
    ("Offline Demonstration", "open", ["physical demonstration", "physical demo", "offline demo",
                                       "in person demo", "on site demo"]),
    ("Sent Quotation", "open", ["quotation sent", "quote sent", "negotiation"]),
    ("Deal PO Released", "won", ["po released", "po received", "won", "client"]),
    ("Ghosted", "nurture", ["no response", "no reply"]),
    ("Rejected", "lost", ["lost", "not interested", "declined", "closed lost"]),
)

# (name, channel, default minutes, counts as a touch on the lead)
ACTIVITY_TYPES: tuple[tuple[str, str, int, bool], ...] = (
    ("Email", "email", 5, True),
    ("Phone call", "phone", 15, True),
    ("WhatsApp message", "whatsapp", 5, True),
    ("WhatsApp call", "whatsapp", 15, True),
    ("WhatsApp group discussion", "whatsapp", 30, True),
    ("Meeting (in person)", "meeting", 60, True),
    ("Meeting (online)", "meeting", 45, True),
    ("Demo", "demo", 60, True),
    ("LinkedIn message", "linkedin", 5, True),
    ("Website enquiry", "web", 5, True),
    ("Other enquiry", "other", 5, True),
    ("Internal discussion", "internal", 30, False),
    ("Follow-up task", "task", 15, False),
    # Written automatically when someone changes a lead's stage, so the timeline shows it.
    ("Stage change", "internal", 0, False),
)

# (name, category, is_mini, delivery days after PO, description, aliases)
# Descriptions stay generic — no hardware specifics.
PRODUCTS: tuple[tuple[str, str, bool, int, str, list[str]], ...] = (
    ("QSim Cluster", "simulator", False, 21,
     "Quantum computing simulator with Demo Notebook Studio notebooks", ["qsim", "qsim cluster", "qc sim"]),
    ("QSim Mini", "simulator", True, 7, "Compact edition of QSim Cluster", ["qsim mini", "qc sim mini"]),
    ("NetSim Q", "simulator", False, 21, "Quantum network simulator", ["netsim", "net sim", "netsimq"]),
    ("NetSim Mini", "simulator", True, 7, "Compact edition of NetSim Q", ["netsim mini", "net sim mini"]),
    ("Quantum Sensing Kit", "kit", False, 70, "Benchtop optical experimentation kit",
     ["sensing kit", "quantum sensing", "atomic physics kit", "qs"]),
    ("Entangled-Photon QKD Kit", "kit", False, 70, "Benchtop optical experimentation kit",
     ["qkd", "qkd kit", "entangled photon kit", "ep-qkd"]),
    ("Quantum Memory Kit", "kit", False, 70, "Benchtop optical experimentation kit",
     ["memory kit", "quantum memory", "qm"]),
)

# Small dropdowns. Each entry: (value, aliases the importer should also recognise).
OPTION_LISTS: dict[str, tuple[tuple[str, list[str]], ...]] = {
    # Institution quality.
    "tier": (
        ("Tier 1", ["1", "t1", "tier1", "tier i"]),
        ("Tier 2", ["2", "t2", "tier2", "tier ii"]),
        ("Tier 3", ["3", "t3", "tier3", "tier iii"]),
    ),
    # Likelihood of closing.
    "potential": (
        ("0%", ["0"]),
        ("30-50%", ["30-50", "30 to 50%"]),
        ("50-70%", ["50-70", "50 to 70%"]),
        ("70-90%", ["70-90", "70 to 90%"]),
        ("100%", ["100"]),
    ),
    "contact_type": (
        ("Faculty", ["professor", "assistant professor", "associate professor", "asst professor",
                     "asst. professor", "lecturer", "faculty member", "scientist"]),
        ("HoD", ["hod", "head of department", "head of the department", "head"]),
        ("Dean", ["dean r&d", "dean research", "dean academics", "associate dean"]),
        ("Director/Principal", ["director", "principal", "vice chancellor", "vc", "pro vice chancellor"]),
        ("Registrar", ["deputy registrar", "assistant registrar"]),
        ("Purchase/Admin", ["purchase", "procurement", "admin", "administration", "store", "stores",
                            "purchase officer", "accounts"]),
        ("Other", []),
    ),
    "department": (
        ("Physics", ["applied physics", "physics and astronomy", "engineering physics"]),
        ("ECE", ["electronics and communication", "electronics and communication engineering",
                 "electronics & communication", "electronics", "ec"]),
        ("EEE", ["electrical and electronics", "electrical and electronics engineering",
                 "electrical", "ee", "electrical engineering"]),
        ("CSE", ["computer science", "computer science and engineering", "cs", "computer engineering",
                 "ai and ds", "ai&ds", "artificial intelligence", "data science", "aiml", "ai & ml"]),
        ("IT", ["information technology"]),
        ("Mechanical", ["mechanical engineering", "mech"]),
        ("Applied Sciences", ["science and humanities", "basic sciences", "applied science",
                              "s&h", "sciences"]),
        ("Mathematics", ["maths", "math", "applied mathematics"]),
        ("Chemistry", ["applied chemistry"]),
        ("Research/Innovation cell", ["r&d", "research", "innovation", "iic", "incubation", "research and development"]),
        ("Administration", ["admin", "office", "institute administration", "college administration",
                            "campus administration"]),
        ("Other", []),
    ),
    # Who brought or works the lead.
    "lead_channel": (
        ("Direct", ["demo", "direct", "in-house"]),
        ("Reseller", ["reseller lead", "partner"]),
        ("Referral", ["referral", "referred"]),
        ("Other", []),
    ),
    # Where a response came from (Responses sheet).
    "response_source": (
        ("Email outreach", ["email", "outreach", "email outreach", "reply", "replied to email"]),
        ("Website enquiry", ["website", "enquiry from website", "enquiries from website",
                             "web", "website form", "contact form"]),
        ("Other", []),
    ),
    # Funding wording that marks a lead as an NQM applicant (sets the lead's NQM flag on import).
    "nqm_funding": (
        ("UG Lab", ["ug lab", "ug labs", "ug teaching lab", "undergraduate lab", "nqm", "nqm ug lab"]),
    ),
    # Knowledge base (Week 5).
    "document_type": (
        ("Brochure", []), ("Spec sheet", []), ("Presentation", []), ("Proposal", []), ("Quotation", []),
        ("Contract / terms", []), ("Case study", []), ("Report", []), ("Video", []), ("Website page", []),
        ("News clipping", []), ("Other", []),
    ),
    # Events & community (Week 9). The level list's aliases are the words that place a person in it, checked in
    # this order (so "Assistant Professor, M.Tech" is Faculty, and "B.Tech, School of ECE" is a UG student).
    "event_kind": (
        ("Hackathon", []), ("Workshop", []), ("Webinar", []), ("FDP", ["faculty development programme"]),
        ("Bootcamp", []), ("Conference", []), ("Expo", []), ("Competition", []), ("Other", []),
    ),
    "community_level": (
        ("PhD scholar", ["phd", "ph.d", "doctoral", "research scholar", "jrf", "srf"]),
        ("Faculty", ["professor", "faculty", "lecturer", "teacher", "hod", "dean", "principal"]),
        ("PG student", ["m.tech", "mtech", "m.sc", "msc", "mca", "mba", "m.e", "postgraduate", "pg", "masters",
                        "master", "m.s"]),
        ("UG student", ["b.tech", "btech", "b.e", "b.sc", "bsc", "bca", "undergraduate", "ug", "bachelor",
                        "bachelors", "diploma", "b.s", "1st year", "2nd year", "3rd year", "4th year",
                        "first year", "second year", "third year", "final year"]),
        ("School student", ["school", "class 11", "class 12", "11th", "12th", "high school"]),
        ("Professional", ["engineer", "developer", "analyst", "scientist", "working", "professional", "consultant",
                          "founder", "manager", "researcher", "intern"]),
        ("Other", []),
    ),
    # Partners, content calendar (2026-10-21).
    "partner_model": (
        ("Reseller", []), ("Distribution partner", ["distributor"]), ("Referral partner", ["referral"]),
        ("Training partner", ["trainer"]), ("Technology partner", ["platform"]), ("Other", []),
    ),
    "content_theme": (
        ("Product", []), ("Student project", []), ("Event", []), ("NQM & policy", []), ("Hiring & careers", []),
        ("Customer story", []), ("Thought leadership", []), ("Behind the scenes", []), ("Other", []),
    ),
    # Hardware specifics that general marketing posts should describe generically (a warning on content posts only;
    # quotations and technical documents keep every detail). aliases[0] = the generic wording to suggest.
    "marketing_hardware_words": (
        ("PPKTP", ["crystal"]), ("BBO crystal", ["crystal"]), ("periodically poled", ["crystal"]),
        ("SNSPD", ["single-photon detector"]), ("SPAD", ["single-photon detector"]),
        ("avalanche photodiode", ["single-photon detector"]), ("Glan-Taylor", ["polariser"]),
        ("Glan-Thompson", ["polariser"]), ("Pockels cell", ["optical modulator"]), ("PBS", ["beam splitter"]),
        ("HWP", ["wave plate"]), ("QWP", ["wave plate"]),
    ),
    # Words that must not appear, each with the wording to use instead (aliases[0]). QSim Cluster runs on Demo's own
    # Notebook Studio. Add words here (Configuration → Sales setup → Content check) only for things that must never
    # be written.
    "flagged_words": (
        ("JupyterHub", ["Demo Notebook Studio"]),
        ("Jupyter Hub", ["Demo Notebook Studio"]),
    ),
    # Procurement (Week 6).
    "funding_source": (
        ("NQM UG teaching lab grant", ["nqm", "ug lab"]), ("Institute funds", []), ("CSR", []),
        ("State scheme", []), ("Other", []),
    ),
    # Demo is not on GeM: public institutions use the PAC route or a tender.
    "buying_route": (
        ("PAC (proprietary article)", ["pac", "proprietary"]), ("Direct purchase order", ["po", "direct"]),
        ("CPPP e-tender", ["cppp", "e-tender"]), ("Open / limited tender", ["tender"]),
    ),
}

# Documents each buying route needs ("Any" applies to every route). Edit on Configuration → Procurement.
PROCUREMENT_RULES: tuple[tuple[str, str, bool], ...] = (
    ("PAC (proprietary article)", "GeM non-availability certificate (from the institution)", False),
    ("PAC (proprietary article)", "Proprietary Article Certificate from the user department", False),
    ("PAC (proprietary article)", "Proprietary certificate from the OEM (Demo)", False),
    ("CPPP e-tender", "Tender document received", False),
    ("CPPP e-tender", "Earnest money deposit (if required)", False),
    ("CPPP e-tender", "Technical and financial bids submitted", False),
    ("Open / limited tender", "Tender / enquiry document received", False),
    ("Open / limited tender", "Bid submitted", False),
    ("Any", "Purchase order received", False),
    ("Any", "Advance payment received (as per payment terms)", False),
    ("Any", "Fall-clause certificate from the reseller", True),
)

# Quotation defaults (Configuration → Quotations). Letterhead, payment terms, support and T&Cs live in the Word
# template (assets/quote_template.docx) and are edited in Word.
APP_SETTINGS: dict[str, str] = {
    "quote_prefix": "DEMO",
    "quote_validity_days": "30",
    "quote_expiry_reminder_days": "5",
    "quote_title": "Quotation for a quantum teaching lab",
    "quote_show_summary": "yes",  # section totals plus a summary table with GST
    # [brackets] = the underlined word in the section heading.
    "section_software_title": "Quantum [Software] licences",
    "section_software_intro": (
        "Simulators for up to 30 students at a time, installed on the institution's server and ready for teaching "
        "within about three weeks of the order."),
    "section_hardware_title": "Benchtop [hardware] kits",
    "section_hardware_intro": (
        "Benchtop experiments in atomic physics, sensing and quantum communication, where students move from "
        "simulation to measurement."),
    "section_other_title": "Services",
    "section_other_intro": "",
    # Shown after the name in "Prepared by" and under the signature; one key per team member's email.
    "title:riya@demo.example": "Head of Academic Partnerships",
    # News & signals (Week 7).
    "news_refresh_hours": "6",   # fetch the feeds again when the last fetch is older than this
    "news_keep_days": "120",     # unread news older than this is cleared out
    "hot_lead_score": "40",      # leads at or above this score are 'hot'
    # Partners & post-sale (2026-10-21).
    "deal_registration_days": "60",   # an approved deal registration is locked to its partner this long
    "distributor_threshold": "5",     # won sales before a reseller can become a distribution partner
    "warranty_months": "12",          # from acceptance
    "amc_rates": "5,7,9,11",          # support from year 2: % of equipment value for years 2, 3, 4, 5
    "warranty_reminder_days": "60",   # remind this many days before the warranty ends
    "amc_reminder_days": "45",        # remind this many days before an AMC ends
    # Privacy requests (DPDP).
    "dpdp_due_days": "30",
}

# Signal types (Week 7): (name, code, points, fades over months, headline words, what it means).
# code is set only for the automatic types the hub works out from its own data. Headline words suggest a type for
# a news item; they are only suggestions — a person always confirms before a signal is saved.
SIGNAL_TYPES: tuple[tuple[str, str | None, int, int, list[str], str], ...] = (
    ("NQM lab approved", None, 30, 12,
     ["nqm lab*", "nqm teaching lab*", "quantum lab sanctioned", "lab approved", "labs approved", "selected under nqm",
      "selected for nqm"],
     "The institution has been approved or selected for an NQM-funded lab."),
    ("Tender / RFQ", None, 30, 12,
     ["tender", "tenders", "rfq", "rfp", "bid", "bids", "expression of interest", "eoi", "procurement"],
     "A tender, RFQ or expression of interest for quantum equipment or software."),
    ("AICTE quantum minor adopted", None, 25, 12,
     ["minor in quantum", "minor degree", "honours in quantum", "aicte quantum", "quantum curriculum"],
     "The institution has adopted the AICTE B.Tech Minor (or honours) in Quantum Technologies."),
    ("New department / centre of excellence", None, 20, 12,
     ["centre of excellence", "center of excellence", "centres of excellence", "quantum centre", "quantum center",
      "new department", "inaugurat*", "launches centre", "launches center", "quantum hub"],
     "A new quantum department, centre or centre of excellence."),
    ("Funding / grant", None, 15, 12,
     ["grant", "grants", "funding", "funded", "crore", "sanctioned", "scheme", "schools"],
     "Funding or a grant for quantum work, from government, industry or CSR."),
    ("FDP / workshop", None, 10, 12,
     ["fdp", "faculty development", "workshop", "bootcamp", "summer school", "hackathon", "training programme"],
     "A faculty development programme, workshop or similar event on quantum."),
    ("Open quotation", "open_quote", 25, 12, [],
     "Automatic: a quotation on one of the lead's deals is sent and still valid."),
    ("Demo held", "demo_held", 20, 12, [],
     "Automatic: a demo was done for the lead (fades from the date of the latest demo)."),
    ("Replied recently", "replied_recently", 15, 3, [],
     "Automatic: the lead wrote to us (fades over 3 months from the latest reply)."),
    ("NQM applicant", "nqm_applicant", 20, 12, [],
     "Automatic: the lead is flagged as an NQM applicant."),
    ("Event participation", None, 10, 12, [],
     "People from the institution took part in one of our events (hackathon, workshop…). Added automatically "
     "from Events once the event has happened."),
)
# Manual types added after the first release: re-added by name to databases seeded earlier.
_ADDED_SIGNAL_TYPES = ("Event participation",)

_GN_INTL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
# News searches (Week 7): (name, kind, search words or RSS address, topic, only keep if it mentions).
# Google News searches use the Indian edition; the two international ones are RSS addresses for the US edition.
NEWS_SOURCES: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("National Quantum Mission", "google_news", '"National Quantum Mission"', "NQM & policy", None),
    ("DST and quantum", "google_news", "DST quantum India", "NQM & policy", None),
    ("PIB press releases", "rss", "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", "NQM & policy",
     "quantum"),
    ("NQM teaching labs", "google_news", '"quantum" "teaching lab" OR "quantum lab" India', "Indian academia",
     None),
    ("AICTE and quantum", "google_news", "AICTE quantum", "Indian academia", None),
    ("Quantum in Indian universities", "google_news", "quantum technology university India", "Indian academia",
     None),
    ("Quantum computing labs in India", "google_news", '"quantum computing" lab India', "Indian academia", None),
    ("Quantum centres of excellence", "google_news", 'quantum "centre of excellence" OR "center of excellence" India',
     "Indian academia", None),
    ("Quantum jobs in India", "google_news", "quantum jobs India", "Jobs & careers", None),
    ("Companies hiring in quantum", "google_news", "quantum hiring OR recruitment", "Jobs & careers", None),
    ("Quantum careers by education level", "google_news",
     '"quantum workforce" OR "quantum careers" OR "quantum skills"', "Jobs & careers", None),
    ("Funding for quantum labs", "google_news", "quantum lab funding OR grant", "Funding & schemes", None),
    ("Government and industry quantum schemes", "google_news", "quantum scheme OR programme India",
     "Funding & schemes", None),
    ("International quantum developments", "rss",
     _GN_INTL.format(q="%22quantum+computing%22+OR+%22quantum+technology%22+when%3A14d"), "International", None),
    ("International quantum education", "rss",
     _GN_INTL.format(q="quantum+education+OR+%22quantum+curriculum%22+OR+%22quantum+course%22+when%3A30d"),
     "International education", None),
)

# Starter objection library: drafts to edit before approving.
OBJECTIONS: tuple[tuple[str, str, str], ...] = (
    ("We don't have budget this year.",
     "Understood. Many institutions start small with one simulator licence and add kits later. Shall I share what "
     "a starter lab would cost, so it can go into next year’s budget?",
     "budget, funding"),
    ("Our purchases have to go through GeM / a tender.",
     "We can work with the route your purchase committee uses — a tender, a direct purchase order or a "
     "proprietary-article purchase — and provide the documents each one needs.",
     "procurement, tender"),
    ("How long does delivery take?",
     "Simulator licences are usually ready within about three weeks of the order; benchtop kits take around ten "
     "weeks. Installation and product training are included.",
     "delivery, timeline"),
    ("We don't have faculty trained in quantum technologies.",
     "Every lab comes with product training and laboratory manuals, and our training partner can run longer "
     "workshops. The labs are designed for your existing faculty.",
     "faculty, training"),
    ("Can we see it before deciding?",
     "Yes — we can set up an online demonstration for your department. Which dates suit you and your colleagues?",
     "demo"),
)

LOST_REASONS: tuple[str, ...] = (
    "Budget",
    "Timing",
    "No faculty",
    "Competitor",
    "Procurement block",
    "Not relevant",
    "Spam / duplicate",
    "Other",
)

# The fictional Demo team. In the demo nobody signs in with a password: visitors pick one of these people.
TEAM: tuple[tuple[str, str, str], ...] = (
    ("Riya Sharma", "riya@demo.example", "admin"),
    ("Kabir Rao", "kabir@demo.example", "member"),
    ("Neha Iyer", "neha@demo.example", "member"),
    ("Arjun Menon", "arjun@demo.example", "member"),
    ("Leadership (view only)", "leadership@demo.example", "viewer"),
)

# Default task owners per track and activity type (the admin changes them on Configuration).
TASK_ASSIGNMENTS: dict[str, dict[str, str]] = {
    "cold_colleges": {"Email": "kabir@demo.example", "LinkedIn message": "riya@demo.example",
                  "Phone call": "riya@demo.example"},
    "referrals": {"Phone call": "riya@demo.example", "Email": "riya@demo.example",
                   "WhatsApp message": "arjun@demo.example"},
    "research_labs": {"Email": "riya@demo.example", "Phone call": "arjun@demo.example"},
    "edtech": {"LinkedIn message": "riya@demo.example", "Email": "riya@demo.example",
                "Phone call": "riya@demo.example"},
    "schools": {"Email": "neha@demo.example", "Phone call": "arjun@demo.example",
            "WhatsApp message": "arjun@demo.example"},
    "inbound_queries": {"Email": "riya@demo.example", "Phone call": "arjun@demo.example",
                        "WhatsApp message": "arjun@demo.example"},
    "individuals": {"Email": "kabir@demo.example", "WhatsApp message": "arjun@demo.example"},
}


def _is_empty(session: Session, model) -> bool:
    return not session.scalar(select(func.count()).select_from(model))


def seed_stages(session: Session) -> int:
    if not _is_empty(session, Stage):
        return 0
    for order, (name, outcome, aliases) in enumerate(STAGES, start=1):
        session.add(Stage(name=name, sort_order=order, outcome=outcome, aliases=aliases))
    return len(STAGES)


def seed_activity_types(session: Session) -> int:
    if not _is_empty(session, ActivityType):
        return 0
    for order, (name, channel, minutes, counts) in enumerate(ACTIVITY_TYPES, start=1):
        session.add(
            ActivityType(
                name=name,
                channel=channel,
                default_duration_min=minutes,
                counts_as_touch=counts,
                sort_order=order,
            )
        )
    return len(ACTIVITY_TYPES)


def seed_products(session: Session) -> int:
    if not _is_empty(session, Product):
        return 0
    for order, (name, category, is_mini, days, description, aliases) in enumerate(PRODUCTS, start=1):
        session.add(
            Product(
                name=name,
                category=category,
                is_mini=is_mini,
                delivery_days=days,
                description=description,
                aliases=aliases,
                sort_order=order,
            )
        )
    return len(PRODUCTS)


def seed_option_lists(session: Session) -> int:
    """Seeds each list only if that list has no values yet."""
    present = set(session.scalars(select(OptionList.list_name).distinct()))
    added = 0
    for list_name, entries in OPTION_LISTS.items():
        if list_name in present:
            continue
        for order, (value, aliases) in enumerate(entries, start=1):
            session.add(OptionList(list_name=list_name, value=value, aliases=aliases, sort_order=order))
            added += 1
    return added


def seed_lost_reasons(session: Session) -> int:
    """Adds reasons that are missing by name (never removes or renames the team's own)."""
    present = set(session.scalars(select(LostReason.name)))
    added = 0
    for order, name in enumerate(LOST_REASONS, start=1):
        if name not in present:
            session.add(LostReason(name=name, sort_order=order))
            added += 1
    return added


def seed_tracks_and_cadences(session: Session) -> tuple[int, int]:
    """Adds missing tracks; adds cadence steps only to tracks that have none yet."""
    types_by_name = {t.name: t for t in session.scalars(select(ActivityType))}
    tracks_by_code = {t.code: t for t in session.scalars(select(Track))}
    tracks_added = 0
    steps_added = 0

    for order, spec in enumerate(TRACKS, start=1):
        track = tracks_by_code.get(spec.code)
        if track is None:
            track = Track(
                code=spec.code,
                name=spec.name,
                goal=spec.goal,
                sender_mailbox=spec.sender_mailbox,
                sub_tiers=spec.sub_tiers,
                notes=spec.notes,
                sort_order=order,
            )
            session.add(track)
            session.flush()
            tracks_added += 1

        has_steps = session.scalar(
            select(func.count()).select_from(CadenceStep).where(CadenceStep.track_id == track.id)
        )
        if has_steps:
            continue  # someone may have edited the cadence; never overwrite it

        for step_no, step in enumerate(spec.steps, start=1):
            missing = [name for name in step.activity_types if name not in types_by_name]
            if missing:
                raise ValueError(f"Cadence step '{step.name}' uses unknown activity types: {missing}")
            session.add(
                CadenceStep(
                    track_id=track.id,
                    step_no=step_no,
                    name=step.name,
                    days_after_previous=step.days_after_previous,
                    grace_days=step.grace_days,
                    complete_on_any=step.complete_on_any,
                    activity_types=[types_by_name[name] for name in step.activity_types],
                )
            )
            steps_added += 1

    return tracks_added, steps_added


def seed_team(session: Session) -> int:
    # Only on a brand-new database, so people added later are never replaced.
    if not _is_empty(session, User):
        return 0
    for name, email, role in TEAM:
        session.add(User(name=name, email=email, role=role))
    return len(TEAM)


def seed_all(session: Session) -> dict[str, int]:
    summary = {
        "stages": seed_stages(session),
        "activity_types": seed_activity_types(session),
        "products": seed_products(session),
        "lost_reasons": seed_lost_reasons(session),
        "option_lists": seed_option_lists(session),
    }
    session.flush()  # cadence steps look up the activity types added just above
    tracks_added, steps_added = seed_tracks_and_cadences(session)
    summary["tracks"] = tracks_added
    summary["cadence_steps"] = steps_added
    summary["users"] = seed_team(session)
    session.flush()
    summary["task_assignments"] = seed_task_assignments(session)
    summary["procurement_rules"] = seed_procurement_rules(session)
    summary["app_settings"] = seed_app_settings(session)
    summary["objections"] = seed_objections(session)
    summary["signal_types"] = seed_signal_types(session)
    summary["news_sources"] = seed_news_sources(session)
    return summary


def seed_signal_types(session: Session) -> int:
    """Seeds every type into an empty table; afterwards only re-adds missing automatic types (by code)."""
    present = {t.code: t for t in session.scalars(select(SignalType))}
    empty = not present
    added = 0
    for order, (name, code, weight, months, words, description) in enumerate(SIGNAL_TYPES, start=1):
        if not empty and (code in present or (code is None and name not in _ADDED_SIGNAL_TYPES)):
            continue
        if not empty and session.scalar(select(SignalType.id).where(SignalType.name == name)):
            continue
        session.add(SignalType(name=name, code=code, weight=weight, decay_months=months, keywords=words or None,
                               description=description, sort_order=order))
        added += 1
    return added


def seed_news_sources(session: Session) -> int:
    if not _is_empty(session, NewsSource):
        return 0
    for order, (name, kind, target, topic, must) in enumerate(NEWS_SOURCES, start=1):
        session.add(NewsSource(name=name, kind=kind, query=target if kind == "google_news" else None,
                               url=target if kind == "rss" else None, topic=topic, must_include=must,
                               sort_order=order))
    return len(NEWS_SOURCES)


def seed_procurement_rules(session: Session) -> int:
    if not _is_empty(session, ProcurementRule):
        return 0
    for order, (route, item, reseller_only) in enumerate(PROCUREMENT_RULES, start=1):
        session.add(ProcurementRule(buying_route=route, item=item, only_if_reseller=reseller_only, sort_order=order))
    return len(PROCUREMENT_RULES)


# Defaults replaced in a later release: swapped only while they still hold the old value (none in the demo yet).
_OLD_DEFAULTS: dict[str, str] = {}
_OLD_FLAGGED: tuple[str, ...] = ()


def seed_app_settings(session: Session) -> int:
    """Adds missing keys; refreshes earlier defaults that were never edited. Edited values are never replaced."""
    rows = {s.key: s for s in session.scalars(select(AppSetting))}
    changed = 0
    for key, value in APP_SETTINGS.items():
        row = rows.get(key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
            changed += 1
        elif key in _OLD_DEFAULTS and row.value == _OLD_DEFAULTS[key]:
            row.value = value
            changed += 1
    # Words an earlier release flagged and a later one allowed again are switched off here.
    for word in session.scalars(select(OptionList).where(OptionList.list_name == "flagged_words",
                                                          OptionList.value.in_(_OLD_FLAGGED),
                                                          OptionList.is_active.is_(True))):
        word.is_active = False
        changed += 1
    return changed


def seed_objections(session: Session) -> int:
    if not _is_empty(session, Objection):
        return 0
    for objection, response, tags in OBJECTIONS:
        session.add(Objection(objection=objection, response=response, tags=tags, status="draft"))
    return len(OBJECTIONS)


def seed_task_assignments(session: Session) -> int:
    """Seeds default task owners for tracks that have none yet (the admin's choices are never overwritten)."""
    configured = set(session.scalars(select(TaskAssignment.track_id).distinct()))
    tracks = {t.code: t for t in session.scalars(select(Track))}
    types = {t.name: t for t in session.scalars(select(ActivityType))}
    users = {u.email.lower(): u for u in session.scalars(select(User))}
    added = 0
    for code, by_type in TASK_ASSIGNMENTS.items():
        for type_name, email in by_type.items():
            track, kind, user = tracks.get(code), types.get(type_name), users.get(email)
            if track and kind and user and track.id not in configured:
                session.add(TaskAssignment(track_id=track.id, activity_type_id=kind.id, user_id=user.id))
                added += 1
    return added


def main() -> None:
    from app.db import session_scope

    with session_scope() as session:
        summary = seed_all(session)

    print("Seed complete. Rows added:")
    for table, count in summary.items():
        print(f"  {table:<16} {count}")
    if not any(summary.values()):
        print("  (nothing new — configuration was already in place)")


if __name__ == "__main__":
    main()
