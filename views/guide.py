"""Guide: how each person uses the hub day to day, and what the words mean."""

from __future__ import annotations

import streamlit as st

from app.ui.common import require_user
from app.ui.layout import page_header

user = require_user()
page_header("Guide", "How the team uses the hub every day. Open the part for your role.", eyebrow="Help")

st.info("This is a portfolio demo. Demo, its team, institutions, contacts and prices are all fictional; the data is "
        "rebuilt every day, so feel free to click anything — log a call, move a stage, approve a post.",
        icon=":material/science:")

st.markdown("#### Every day, everyone")
st.markdown(
    "1. Open **Today**. Work through *Overdue*, then *Due today*. **Done** asks what happened and what's next; "
    "**Reschedule** moves it; **More → Skip** counts as handled; **More → Hand off** passes it to a teammate "
    "with a note.\n"
    "2. Talked to someone outside a task — a call, WhatsApp, meeting? Use **New → Log activity** (top right). "
    "Add a next step so nothing is forgotten.\n"
    "3. Anything to remember for later goes in **New → Task** — with or without an institution.\n"
    "4. **Calendar** shows the same tasks on a week grid; **Add to my calendar (.ics)** puts meetings into "
    "Google Calendar or Outlook.\n"
    "5. Need a brochure, script or an answer to an objection? **Library**. Send only *Approved* versions, and pick them "
    "under **Documents shared** when you log the email or meeting. Anyone can add a link; you can remove your own "
    "drafts.\n"
    "6. Glance at **News & signals** (the number beside it = new news naming one of our leads). If an item matters — "
    "an NQM lab, a tender, a new centre — press **Save as signal**: the lead's score rises and it moves up Today. "
    "**Not relevant** clears it; **Save to Library** keeps the article as a News clipping.\n"
    "7. Before Friday, write your **Notes for this week's report** at the bottom of Today — highlights, blockers, "
    "anything leadership should know. Your **Targets this week** show under *Your week*."
)

with st.expander("Neha — fresh lead generation and verification", icon=":material/person_search:"):
    st.markdown(
        "- **Colleges** → filter (state, type, *Not a lead yet*) → **Download research batch**.\n"
        "- Research contacts with AI as usual, then **Import → Paste rows** (copy the AI's table including the "
        "header row). The paste box shows the exact column headers to ask the AI for.\n"
        "- **Contacts** → filter Track *Colleges — cold outreach* → tick the rows you've checked → "
        "**Mark verified**. Verified contacts can enter the cadence (automatically, if auto-start is on)."
    )
with st.expander("Kabir — email automation, referral verification", icon=":material/outgoing_mail:"):
    st.markdown(
        "- **Contacts** → Track *Referrals and past contacts* → tick checked rows → **Mark verified**.\n"
        "- The email batch includes a ready **Subject** and **Body** for every row whose cadence step has an approved "
        "script in the Library — paste those columns into your mail-merge sheet if it reads them.\n"
        "- **Today** → *Email batch for Google Sheets* → **Download CSV** or **Copy for Google Sheets** → paste "
        "into your mail-merge sheet → send → back in the hub, **Mark these as sent**. Each cadence moves on.\n"
        "- An email came back undeliverable? On the task choose **Done → The email bounced**, or on Contacts tick "
        "the person → **Email bounced**.\n"
        "- Start cadences for newly verified leads from **Leads** (tick leads → **Start cadence**)."
    )
with st.expander("Events, community and GTM charts — everyone", icon=":material/event:"):
    st.markdown(
        "- **Events → New event** for a hackathon, workshop, webinar or FDP. Add two questions to its Google Form: "
        "*I am 18 or older* and *I agree to be contacted by Demo*.\n"
        "- After registrations: Google Forms → **Responses → ⋮ → Download responses (.csv)** → open the event → "
        "**Import responses** → check the matched questions → **Import**. People are recognised by email, so the "
        "same person across events stays one member.\n"
        "- On the event, set **Status** (attended, finalist, winner), teams and project links, then mark the event "
        "**Done**: each participant's institution that is a lead gets an *Event participation* signal (+10).\n"
        "- **Community** finds people by level, interests, institution or event. **Mailing list** and **Copy "
        "emails** include only people who agreed to be contacted and are 18 or older. **Create lead** turns a "
        "member's institution into a lead. Deletion requests: profile → More → Delete (admin).\n"
        "- **GTM charts → GTM map** is drawn from the cadences in Configuration, with live counts. **Diagram "
        "builder**: start from a template (partner model, reseller model, a track's cadence) or type arrows, e.g. "
        "`Email -> Call -> Demo`; save and download as PNG, SVG or PDF. **Institute categories** compares public/"
        "private and urban/rural institutions on leads, replies, demos, quotes and wins."
    )
with st.expander("Content calendar — LinkedIn and Instagram", icon=":material/campaign:"):
    st.markdown(
        "- **Content → New idea**: a working title is enough. Add the text, theme and planned date when you're ready, "
        "then **Send for approval**. Ideas also start from **Make a post** on a news item or an event.\n"
        "- The admin sees *Check before approving* (kit part names, JupyterHub, length limits) and either "
        "**Approves** (date + who posts it) or **Sends back** with a comment.\n"
        "- On the day, the poster gets a *Post on LinkedIn* task on Today: open the post → copy the text → post it "
        "on LinkedIn/Instagram → **Mark as posted** with the link.\n"
        "- A week later Today asks for the numbers: open the post → enter **likes, comments, reposts, impressions** "
        "→ **Save numbers**. **What works** (and the weekly report) then shows which themes do best.\n"
        "- Changing the text of an approved post sends it back for approval, so what goes out is always what was "
        "approved."
    )
with st.expander("Partners, clients and privacy requests", icon=":material/handshake:"):
    st.markdown(
        "- **Partners → New partner** for each reseller, distribution, referral, training or technology partner: model, "
        "NDA date, non-compete, price list version, payment terms, territory; link the signed NDA from the Library.\n"
        "- When a partner tells us about an institution: **Register a deal** → pick partner and institution — the "
        "hub shows at once if it's already our lead or another partner's. An admin **Approves** (the lead is linked "
        "to the partner, channel Reseller, locked for 60 days) or **Rejects** with a reason. After 5 won sales a "
        "reseller shows *Ready to upgrade*.\n"
        "- **Clients** starts automatically when a PO is received. Fill in delivered / installed / accepted dates "
        "and the product training date; the warranty end is worked out (12 months from acceptance). Today reminds "
        "the lead owner about training, 60 days before the warranty ends (offer the AMC — suggestions at 5/7/9/11% "
        "are on the page), 45 days before an AMC ends, and on the upsell follow-up date.\n"
        "- **Privacy requests**: someone asks what data we hold, to be deleted, or to stop being contacted (an "
        "*unsubscribe* reply counts) → **Log a request** the same day. Open it → see their records → **Export their "
        "data**, **Stop contacting**, or (admin) **Delete everywhere** → write what was done → **Done**. It's due in "
        "30 days; overdue ones are flagged on the Dashboard and in the menu."
    )
with st.expander("Riya (admin) — calls, demos, queries, partners", icon=":material/support_agent:"):
    st.markdown(
        "- **New → Query** (or Queries → **New query**) for each enquiry arriving at hello@demo.example (or WhatsApp/phone). "
        "A *Reply* task is created, due the same day and overdue after 1 day. The follow-ups set for the Website "
        "enquiries track then run by themselves.\n"
        "- When it's clear what they need, **Move to track** (e.g. Individual learners). Spam or not relevant → "
        "**Close query**.\n"
        "- Calls and demos appear on **Today**. *Cadence finished, no reply* asks whether to **Mark Ghosted** "
        "(the date is recorded).\n"
        "- **Quotes:** lead → **Deals** → **New deal** → **New quote** → **Word file** (check in Word, *Save as PDF*, "
        "send) → **Mark as sent**. When the PO arrives: **PO received**.\n"
        "- **Library:** approve documents, scripts and objection answers once the wording is checked.\n"
        "- **Configuration**: who gets which cadence task, auto-start, daily cap; **Sales setup** for prices, bundles, "
        "the quotation template and the flagged-words list; **News & signals** for the news searches, signal points "
        "and the hot score — and **Prepare full backup** every Friday.\n"
        "- **Every Monday** Today shows *Prepare last week's report and set this week's targets*: open **Weekly "
        "report** → check the numbers → add highlights, risks and support needed → **Finalise** (freezes the "
        "numbers) → **Word report** for leadership. Then **Targets** tab → adjust (it starts from last week's) → "
        "**Save targets**.\n"
        "- **Signals in bulk:** News & signals → **Add signals** → paste the institutions named in a PIB release "
        "(one per line) or upload a sheet → check the matches → save."
    )

st.markdown("#### What the words mean")
st.markdown(
    "| Word | Meaning |\n|---|---|\n"
    "| **Track** | The kind of lead and its goal: colleges, referrals, research institutes, EdTech platforms, "
    "schools, website enquiries, individual learners. |\n"
    "| **Stage** | How far the conversation has got: New → First Contact Made → Replied to Queries → Sent Brochure "
    "→ demos → Sent Quotation → Deal PO Released; or Ghosted / Rejected. |\n"
    "| **Cadence** | The follow-up routine for a track; the hub plans each step and stops when they reply. |\n"
    "| **Verified** | Someone checked the email is right. Only verified contacts get cadence emails. |\n"
    "| **Bounced** | An email came back undeliverable; that address gets no more emails. |\n"
    "| **Ghosted** | Every follow-up was done and they never replied. |\n"
    "| **Do not contact** | They asked not to be contacted; no import or cadence will reach them again. |\n"
    "| **Signal** | A dated fact that makes a sale more likely — NQM lab approved, tender, new centre, grant, FDP. "
    "Worth points that fade to zero over 12 months. |\n"
    "| **Lead score** | The institution's signals plus automatic points (open quote, recent reply, demo, NQM flag). "
    "**Hot** = at or above the hot score (40). |\n"
    "| **Outreach** | Institutions contacted for the first time that week. |\n"
    "| **Follow-up** | Any later touch — calls, emails, WhatsApp, LinkedIn, meetings. |\n"
    "| **Community member** | Someone who took part in one of our events; recognised by email. |\n"
    "| **Mailable** | A community member who agreed to be contacted and confirmed they're 18 or older. |\n"
    "| **Deal registration** | A partner's claim on an institution; once approved, no one else works it for 60 "
    "days. |\n"
    "| **AMC** | Annual maintenance contract — support after the warranty, priced as a % of the equipment value. |\n"
    "| **Engagements** | Likes + comments + reposts on a post. |\n"
    "| **DPDP request** | A person using their rights under India's data protection law: see, correct or delete "
    "their data, or stop being contacted. |"
)

st.markdown("#### Getting around")
st.markdown(
    "- **Keyboard:** Tab moves between controls, Enter presses a button, Esc closes a pop-up.\n"
    "- **Bigger text:** use your browser's zoom (Ctrl + / Ctrl −); the layout adapts.\n"
    "- **Dark mode:** ⋮ (top right) → Settings → Theme.\n"
    "- **Search:** the box at the top of every page finds leads, contacts and colleges by name, email, phone or "
    "AISHE code.\n"
    "- **Alerts (bell):** tasks handed to you, queries past the 1-day reply target, finished cadences.\n"
    "- **On a phone:** the menu is behind » at the top left; search, alerts and **New** stay at the top.\n"
    "- **Lead record:** tick a lead on Leads → **Open lead record**, or follow *Open lead* from Today, search, "
    "alerts, Queries or Calendar. **All leads** takes you back."
)
