# BD & Marketing Hub — portfolio demo

A working copy of an internal business-development hub I built for a deep-tech company that sells quantum
teaching labs (simulators and benchtop experimentation kits) to universities in India, and quantum upskilling to
companies and public bodies. Everything a small BD team
does — leads, follow-up cadences, calls, emails, WhatsApp and meetings, quotations, partners, events, LinkedIn
posts, news signals and the weekly leadership report — happens in one app.

**This repository is the demo build.** The code and features are the real thing; the company (**Demo**), its team,
organisations, contacts and prices are fictional, and the data rebuilds itself every day.

- **Live demo:** _add your Streamlit link here_ — pick a persona (Admin, Member or Viewer) and click anything.
- **Built by:** Mekhala Ganguly

![Today page](docs/screenshots/today.png)

## What it does

| Area | In the app |
|---|---|
| CRM | Any organisation — universities, companies, schools, research labs, public bodies — filtered by **industry** and type; contacts and leads across eight lead tracks; safe spreadsheet imports (preview, duplicate matching, undo) and editable tables with an audit log |
| Outreach | Cadences plan every follow-up, step by step for each track; a reply stops them; **Today** shows each person's overdue and due work; a calendar; an email batch for a mail-merge sheet; bounce handling |
| Timeline | Every call, email, WhatsApp, meeting and demo on one lead record, with next steps and hand-offs |
| Knowledge | A library of links with versions and admin approval, scripts with fill-in fields tied to cadence steps, an objection library |
| Sales | Deals with procurement checklists by buying route, a price list and bundles, quotations generated as Word files |
| Partners & clients | Reseller deal registration with an overlap check and a 60-day lock; post-sale delivery, installation, warranty and AMC reminders |
| Marketing | Events and a community database (Google Forms import, consent rules), a LinkedIn/Instagram content calendar with approval and engagement tracking, GTM charts drawn from the cadence settings |
| Intelligence | Free news feeds tagged by topic; signals (NQM lab approved, tender, new centre…) that score leads and order Today |
| Reporting | Dashboard by Indian financial year; a weekly leadership report with targets, frozen when final, downloadable as Word and Excel |
| Privacy | DPDP requests: find every record for an email, export, stop contact or delete everywhere, with a 30-day due date |

## How it's built

- **Python + Streamlit** for the interface, with a custom theme and shared components; checked against WCAG 2.1 AA.
- **SQLAlchemy 2** models as the backbone, **Alembic** migrations for Postgres in production, **SQLite** for this demo.
- **Configuration as data**: tracks, cadences, stages, activity types, products, prices, signal weights and dropdowns
  are tables the team edits, not code.
- **python-docx / openpyxl / matplotlib** for quotations, the weekly report, exports and charts.
- **150+ pytest checks**, including guards that the demo contains no real names, emails or phone numbers.

### How the demo data works

`app/demo/build.py` moves a clock (freezegun) day by day through the past twelve weeks while `app/demo/story.py`
calls the app's **own services**: Neha imports research batches, cadences start and run, the team works its tasks,
some organisations reply, demos are booked, quotes go out, POs arrive, posts are approved and published, and every
Monday the weekly report is finalised. So every timeline, stage history, score and report is exactly what the real app
would have produced. The run is seeded (the same day always builds the same world) and takes about 20 seconds.

The app rebuilds the database once a day (IST), so the story always ends *today* and visitors' changes reset. The
rebuild runs on a background thread: visitors keep using yesterday's data and today's is swapped in when it's ready.
Only a server that has just woken up has to wait, and its build starts while the visitor is still on the welcome page.
Pages cache their tables; any save clears every cache (a counter in `app/db.py` goes up on each commit that wrote
rows), so cached pages are fast and never out of date. Every email is on the reserved `.example` domain and every phone number is `+91 0…`, which no Indian
mobile uses.

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
streamlit run streamlit_app.py     # builds data/demo.db on first open
pytest                             # all checks, including the demo guards
```

Build the data by hand (any date): `python -m app.demo.build --today 2026-10-22`.

| Setting (env var or Streamlit secret) | Default | What it does |
|---|---|---|
| `DEMO_MODE` | `1` | Persona picker instead of sign-in. `0` gives the original Google sign-in. |
| `DATABASE_URL` | *(none: SQLite in `data/`)* | Use a throwaway Postgres instead; fill it with `python -m app.demo.build --url …` |
| `DEMO_AUTHOR`, `DEMO_AUTHOR_URL`, `DEMO_REPO_URL` | author name only | Credits and links on the welcome and About pages |

## Project layout

```
streamlit_app.py      Entry point: daily data refresh, persona picker, menu, then the chosen page
views/                The pages (Today, Leads, Quotes, Weekly report, GTM charts, About this demo…)
app/models/           Tables (SQLAlchemy)
app/services/         Business logic, kept separate from the screens
app/ui/               Shared layout and components
app/seed/             Starting configuration (Demo's tracks, cadences, products, settings)
app/demo/             The demo: data builder, the twelve-week story, word pools, personas, daily refresh
assets/               Demo's logo and the quotation template
tools/                Scripts that drew the logo and the quotation template
alembic/              Database migrations (Postgres)
tests/                pytest checks (run on a throwaway database)
```

See **DEPLOY.md** to put the demo online.

## License

[MIT](LICENSE) © 2026 Mekhala Ganguly. The libraries this app uses keep their own licences. Fonts are loaded from
Google Fonts (SIL Open Font License).
