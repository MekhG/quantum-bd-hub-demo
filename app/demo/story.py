"""Twelve weeks in the life of Demo's BD team — played through the hub's own services.

The Story sets up the configuration (prices, library, partners), adds a little earlier history (three clients from
last year), and then walks day by day to today: Neha imports and verifies leads, cadences start and run, Kabir and
Riya work their tasks, some institutions reply, demos are booked, quotes go out, POs arrive, posts are approved and
published, events happen, and every Monday the weekly report is finalised. Randomness is seeded, so a given date
always produces the same data. Nothing here is real: every institution, person and price is invented.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from random import Random

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.demo import names as N
from app.models import (
    Activity,
    AuditLog,
    CommunityMember,
    ContentPost,
    Contact,
    DataRequest,
    Document,
    EventParticipant,
    Installation,
    Lead,
    Objection,
    Opportunity,
    Organisation,
    PriceItem,
    Product,
    Quote,
    Script,
    SignalType,
    Stage,
    Track,
    User,
)
from app.seed.seed_config import seed_all
from app.services import cadence as C
from app.services.activity_log import LogEntry, NextStep, finish_task, log_activity
from app.services.cadence import IST, at_ist, ist_date

DOC_BASE = "https://example.com/demo-library/"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Story:
    def __init__(self, session: Session, *, clock, today: date, first_day: date, rng: Random) -> None:
        self.s = session
        self.clock = clock
        self.today = today
        self.first = first_day
        self.rng = rng
        self.agenda: list[tuple[date, int, int, str]] = []
        self._seq = 0
        self.warmth: dict[int, float] = {}
        self.counts: Counter = Counter()
        self.emails: set[str] = set()
        self.pool: list[dict] = []
        self.free_codes: list[str] = []
        self.docs: dict[str, Document] = {}
        self.quotes_by_lead: dict[int, int] = {}
        self.log: Callable[..., None] = lambda *a, **k: None

    # ================================================================== small helpers

    def at(self, day: date, hour: int = 10, minute: int = 0) -> datetime:
        return self.clock.at(day, hour, minute)

    def work_time(self, day: date) -> datetime:
        return self.at(day, self.rng.randint(10, 17), self.rng.choice((0, 10, 20, 30, 40, 50)))

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def plan(self, day: date, lead_id: int, action: str) -> None:
        self._seq += 1
        self.agenda.append((day, self._seq, lead_id, action))

    def track(self, code: str) -> Track:
        return self._tracks[code]

    def stage(self, name: str) -> Stage:
        return self._stages[name]

    def product(self, name: str) -> Product:
        return self._products[name]

    def lead_of(self, lead_id: int) -> Lead:
        return self.s.get(Lead, lead_id)

    def is_closed(self, lead: Lead) -> bool:
        return lead.stage.outcome in ("won", "lost")

    def owner_of(self, lead: Lead) -> User:
        return self.s.get(User, lead.owner_user_id) if lead.owner_user_id else self.riya

    def person(self) -> tuple[str, str]:
        return self.rng.choice(N.FIRST_NAMES), self.rng.choice(N.LAST_NAMES)

    def email_for(self, first: str, last: str, domain: str) -> str:
        base = f"{first}.{last}".lower().replace("'", "").replace(" ", "")
        email, n = f"{base}@{domain}", 1
        while email in self.emails:
            n += 1
            email = f"{base}{n}@{domain}"
        self.emails.add(email)
        return email

    def phone(self) -> str:
        # +91 followed by 0: no Indian mobile number starts with 0, so these can never ring a real phone.
        return f"+91 0{self.rng.randint(1000, 9999)} {self.rng.randint(10000, 99999)}"

    def weighted(self, pairs) -> str:
        values, weights = zip(*pairs, strict=True)
        return self.rng.choices(values, weights=weights)[0]

    # ================================================================== the whole run

    def run(self, log: Callable[..., None] | None = None) -> dict:
        self.log = log or self.log
        start = self.first - timedelta(days=400)
        self.at(start, 9)
        seed_all(self.s)
        self.s.flush()
        self._load_refs()
        self._configure(start)
        self._price_list(start)
        self._library(start)
        self._partners(start + timedelta(days=20))
        self._pool(start + timedelta(days=2))
        self._enterprise_pool(start + timedelta(days=3))
        self._history_clients()
        self._other_tracks()
        self._first_imports(self.first)
        self.log(f"Setup done; simulating {self.first} → {self.today}")

        day = self.first
        while day <= self.today:
            self._day(day)
            day += timedelta(days=1)
        self._finish()
        totals = {
            "institutions": self.s.scalar(select(func.count(Organisation.id))),
            "leads": self.s.scalar(select(func.count(Lead.id))),
            "contacts": self.s.scalar(select(func.count(Contact.id))),
            "activities": self.s.scalar(select(func.count(Activity.id))),
            "quotes": self.s.scalar(select(func.count(Quote.id))),
            "won": self.s.scalar(select(func.count(Opportunity.id)).where(Opportunity.status == "won")),
        }
        return totals

    def _load_refs(self) -> None:
        users = {u.email.split("@")[0]: u for u in self.s.scalars(select(User))}
        self.riya, self.kabir, self.neha, self.arjun = users["riya"], users["kabir"], users["neha"], users["arjun"]
        self.team = [self.riya, self.kabir, self.neha, self.arjun]
        self._tracks = {t.code: t for t in self.s.scalars(select(Track))}
        self._stages = {st.name: st for st in self.s.scalars(select(Stage))}
        self._products = {p.name: p for p in self.s.scalars(select(Product))}
        self._signal_types = {t.name: t for t in self.s.scalars(select(SignalType))}

    def _configure(self, day: date) -> None:
        # College leads start their cadence by themselves once a contact is verified, at most 6 a day.
        fresh = self.track("cold_colleges")
        fresh.auto_start_cadence, fresh.daily_start_cap = True, 6
        # Enterprise leads start by themselves too once LinkedIn confirms the person, at most 3 a day.
        enterprise = self.track("enterprise")
        enterprise.auto_start_cadence, enterprise.daily_start_cap = True, 3
        research, edtech = self.track("research_labs"), self.track("edtech")
        research.sender_mailbox = edtech.sender_mailbox = "partners@demo.example"
        self.track("schools").sender_mailbox = "riya@demo.example"
        for obj in self.s.scalars(select(Objection)):
            from app.services.library import set_objection_status

            set_objection_status(self.s, obj.id, "approved", self.riya)
        self.s.flush()

    # ================================================================== price list, bundles, library, partners

    PRICES = (  # (name, product, unit, institute ₹, partner ₹, section description)
        ("QSim Cluster — 30-seat teaching licence", "QSim Cluster", "licence", 1100000, 900000,
         "Quantum computing simulator for up to 30 concurrent students, with Demo Notebook Studio courseware "
         "and 40 guided exercises."),
        ("NetSim Q — department licence", "NetSim Q", "licence", 950000, 780000,
         "Quantum network simulator for entanglement distribution, BBM92 and network-protocol labs."),
        ("QSim Mini — individual licence (1 year)", "QSim Mini", "licence", 45000, 36000,
         "Single-user edition of QSim Cluster for students and researchers."),
        ("NetSim Mini — individual licence (1 year)", "NetSim Mini", "licence", 40000, 32000,
         "Single-user edition of NetSim Q."),
        ("Quantum Sensing Kit", "Quantum Sensing Kit", "kit", 2450000, 2000000,
         "Benchtop kit for atomic physics and magnetometry experiments with rubidium vapour, including EIT and the "
         "Zeeman effect."),
        ("Entangled-Photon QKD Kit", "Entangled-Photon QKD Kit", "kit", 2800000, 2300000,
         "Benchtop kit for entangled photon pairs and BBM92 key distribution experiments."),
        ("Quantum Memory Kit", "Quantum Memory Kit", "kit", 3200000, 2650000,
         "Benchtop kit for light storage and retrieval experiments in an atomic medium."),
        ("Installation and commissioning", None, "visit", 75000, 60000,
         "On-site installation, alignment and acceptance testing."),
        ("Faculty product training (2 days)", None, "batch", 50000, 40000,
         "Hands-on training for the faculty who will run the laboratory."),
        ("Team upskilling workshop (2 days)", None, "batch", 180000, 150000,
         "Instructor-led quantum computing and quantum-safe networking workshop for up to 30 engineers."),
    )

    def _price_list(self, day: date) -> None:
        self.at(day, 11)
        self.price_items: dict[str, PriceItem] = {}
        for order, (name, product, unit, inst, partner, desc) in enumerate(self.PRICES, start=1):
            item = PriceItem(product_id=self.product(product).id if product else None, name=name, description=desc,
                             unit=unit, institute_price_inr=Decimal(inst), partner_price_inr=Decimal(partner),
                             gst_pct=Decimal("18"), hsn_sac="998314" if product is None or "Sim" in name else "9027",
                             valid_from=day, sort_order=order)
            self.s.add(item)
            self.price_items[name.split(" —")[0]] = item
        self.s.flush()
        from app.services.procurement import save_bundle

        p = self.price_items
        self.bundles = {
            "phase1": save_bundle(self.s, name="Starter simulation lab", phase="Starter",
                                  description="Start teaching within weeks: both simulators plus faculty training.",
                                  items=[(p["QSim Cluster"].id, 1), (p["NetSim Q"].id, 1),
                                         (p["Faculty product training (2 days)"].id, 1)], user=self.riya),
            "phase2": save_bundle(self.s, name="Quantum communication add-on", phase="Add-on",
                                  description="Entangled-photon experiments for final-year projects.",
                                  items=[(p["Entangled-Photon QKD Kit"].id, 1),
                                         (p["Installation and commissioning"].id, 1)], user=self.riya),
            "phase3": save_bundle(self.s, name="Sensing and memory add-on", phase="Add-on",
                                  description="Atomic physics, sensing and memory experiments.",
                                  items=[(p["Quantum Sensing Kit"].id, 1), (p["Quantum Memory Kit"].id, 1),
                                         (p["Installation and commissioning"].id, 1)], user=self.riya),
        }

    DOCS = (  # key, title, type, products, description, key text
        ("brochure_qsim", "QSim Cluster brochure", "Brochure", ["QSim Cluster"],
         "Two-page brochure for heads of department.", "30 students at a time; notebooks; 40 exercises; server install"),
        ("brochure_netsim", "NetSim Q brochure", "Brochure", ["NetSim Q"],
         "Two-page brochure for ECE and CSE departments.", "entanglement distribution; BBM92; network protocols"),
        ("brochure_kits", "Experimentation kits — tri-fold brochure", "Brochure",
         ["Quantum Sensing Kit", "Entangled-Photon QKD Kit", "Quantum Memory Kit"],
         "Tri-fold for physics departments.", "benchtop kits; rubidium vapour; magnetometry; entangled photon pairs"),
        ("spec_qsim", "QSim Cluster — specification sheet", "Spec sheet", ["QSim Cluster"],
         "Server requirements and course coverage.", "GPU server; 30 concurrent users; Demo Notebook Studio"),
        ("spec_sensing", "Quantum Sensing Kit — specification sheet", "Spec sheet", ["Quantum Sensing Kit"],
         "Experiments and bench requirements.", "EIT; Zeeman effect; laser locking; magnetometry"),
        ("proposal", "Quantum teaching lab — proposal", "Proposal", [],
         "Options for a starter lab and add-ons.", "starter simulation lab; communication add-on; sensing add-on"),
        ("reseller_terms", "Standard reseller terms", "Contract / terms", [],
         "NDA and payment terms for resellers.", "partner prices; deal registration"),
        ("video", "Demo overview video (3 min)", "Video", [],
         "Short overview for faculty and students.", "what students learn; how the lab is delivered"),
        ("case_study", "Case study: Kalpavriksha Institute of Technology", "Case study", ["QSim Cluster", "NetSim Q"],
         "How a starter lab went from PO to first class in 6 weeks.", "reference client; simulation lab"),
    )

    def _library(self, day: date) -> None:
        from app.services.library import (
            DocumentData,
            ScriptData,
            add_document,
            add_script,
            approve_document,
            approve_script,
            new_document_version,
        )

        self.at(day, 12)
        for key, title, kind, products, desc, key_text in self.DOCS:
            data = DocumentData(title=title, doc_type=kind, url=f"{DOC_BASE}{_slug(title)}.pdf", description=desc,
                                key_text=key_text, tags=", ".join(p.lower() for p in products) or None,
                                version_label="v1", product_ids=[self.product(p).id for p in products])
            doc, _ = add_document(self.s, data, self.kabir if kind == "Brochure" else self.riya)
            if key != "case_study":  # the case study stays a draft waiting for approval
                approve_document(self.s, doc.id, self.riya)
            self.docs[key] = doc
        # A second version of the QSim brochure, approved later: the Library shows the history.
        self.at(day + timedelta(days=200), 12)
        first = self.docs["brochure_qsim"]
        newer, _ = new_document_version(self.s, first.id, DocumentData(
            title=first.title, doc_type="Brochure", url=f"{DOC_BASE}qsim-cluster-brochure-v2.pdf",
            description="Updated with the new exercise list.", key_text=first.key_text, tags=first.tags,
            version_label="v2", product_ids=[self.product("QSim Cluster").id]), self.kabir)
        approve_document(self.s, newer.id, self.riya)
        self.docs["brochure_qsim"] = newer

        steps = {s.step_no: s for s in self.track("cold_colleges").cadence_steps} \
            if hasattr(self.track("cold_colleges"), "cadence_steps") else {}
        if not steps:
            from app.models import CadenceStep

            steps = {s.step_no: s for s in self.s.scalars(select(CadenceStep).where(
                CadenceStep.track_id == self.track("cold_colleges").id))}
        fresh = self.track("cold_colleges").id
        scripts = (
            ("Colleges — intro email", "email", 1, "A quantum teaching lab for {organisation}",
             "{salutation} {last_name},\n\nI lead academic partnerships at Demo. We help departments like yours teach "
             "quantum computing with simulators your students can use from day one.\n\n"
             "Would a 30-minute online demo for your {department} faculty be useful?\n\nBest regards,\n{sender_name}"),
            ("Colleges — case study email", "email", 3, "How one college started teaching quantum in six weeks",
             "{salutation} {last_name},\n\nSharing a short case study of a starter lab that went from order to first "
             "class in six weeks, using {product}. If the timing isn't right, just let me know.\n\n{sender_name}"),
            ("Colleges — break-up email", "email", 4, "Closing the loop",
             "{salutation} {last_name},\n\nI'll close this thread for now so I don't crowd your inbox. If a quantum "
             "lab comes up in your plans, I'd be glad to help.\n\nWarm regards,\n{sender_name}"),
        )
        for title, kind, step_no, subject, body in scripts:
            script, _ = add_script(self.s, ScriptData(title=title, kind=kind, body=body, subject=subject,
                                                      track_id=fresh, cadence_step_id=steps[step_no].id), self.kabir)
            approve_script(self.s, script.id, self.riya)
        extra = (
            ("Call opener — heads of department", "call",
             "Hello {salutation} {last_name}, this is {sender_name} from Demo. I wrote to you about a quantum teaching "
             "lab for {organisation}. Do you have two minutes? I'd like to understand what your department teaches "
             "today and whether a short demo would help."),
            ("WhatsApp — after a missed call", "whatsapp",
             "Hello {salutation} {last_name}, {sender_name} from Demo here. I tried calling about the quantum lab for "
             "{organisation}. When would be a good time to talk?"),
        )
        for title, kind, body in extra:
            script, _ = add_script(self.s, ScriptData(title=title, kind=kind, body=body), self.riya)
            approve_script(self.s, script.id, self.riya)
        self.s.flush()

    def _partners(self, day: date) -> None:
        from app.services.partners import PartnerData, save_partner

        self.at(day, 11)
        self.partners = {}
        specs = (
            ("northwind", "Northwind Lab Supplies", "Reseller", "active", True, "South India — Telangana, Andhra "
             "Pradesh, Karnataka", "50% advance with the PO, balance on delivery (resellers pay Demo directly)."),
            ("brightpath", "Brightpath Skills Foundation", "Training partner", "active", False, "Pan-India",
             "Revenue share per programme, agreed per FDP."),
            ("coastal", "Coastal Edu Partners", "Referral partner", "prospect", False, "Kerala and Tamil Nadu",
             None),
        )
        for key, name, model, status, nda, territory, terms in specs:
            partner = save_partner(self.s, PartnerData(
                model=model, new_org_name=name, status=status, nda_signed_on=day if nda else None, non_compete=nda,
                nda_doc_id=self.docs["reseller_terms"].id if nda else None,
                terms_doc_id=self.docs["reseller_terms"].id if model == "Reseller" else None,
                price_list_version="2026.1" if model == "Reseller" else None, payment_terms=terms,
                owner_id=self.riya.id, territory=territory), self.riya)
            org = self.s.get(Organisation, partner.org_id)
            org.website, org.domain = f"https://www.{_slug(name)}.example", f"{_slug(name)}.example"
            if key == "brightpath":
                org.industry = "Non-profit & CSR"
            first, last = self.person()
            contact = Contact(org_id=org.id, name=f"{first} {last}", designation="Managing Director",
                              contact_type="Founder/CXO", email=self.email_for(first, last, org.domain),
                              phone=self.phone(), verified=True,
                              verified_on=day, verified_by_user_id=self.riya.id, source="Partner onboarding")
            self.s.add(contact)
            self.s.flush()
            partner.contact_id = contact.id
            self.partners[key] = partner
        self.s.flush()

    # ================================================================== the prospect pool (raw college list)

    def _pool(self, day: date) -> None:
        """~300 institutions imported as a raw list, like the real hub's AISHE pool (but every one invented)."""
        from app.services.importing.runner import ImportOptions, run_import

        self.at(day, 15)
        prefixes = list(N.PREFIXES)
        self.rng.shuffle(prefixes)
        states = list(N.STATE_WEIGHTS)
        weights = list(N.STATE_WEIGHTS.values())
        rows, names = [], set()
        universities: list[str] = []

        def add(kind: str, number: int, prefix: str, suffix: str) -> None:
            name = f"{prefix} {suffix}"
            if name in names:
                return
            names.add(name)
            state = self.rng.choices(states, weights=weights)[0]
            city = self.rng.choice(N.STATES[state])
            code = {"university": f"U-9{number:03d}", "college": f"C-9{number:04d}",
                    "standalone": f"S-9{number:04d}"}[kind]
            domain = f"{_slug(prefix)}-{''.join(w[0] for w in suffix.split() if w[0].isalpha()).lower()}.example"
            management = self.weighted(N.MANAGEMENT)
            entry = {
                "code": code, "name": name, "kind": kind, "state": state, "city": city, "domain": domain,
                "area": "Rural" if self.chance(0.32) else "Urban", "management": management,
                "category": self.rng.choice(N.COLLEGE_CATEGORIES) if kind == "college" else
                ("Institute" if kind == "standalone" else None),
                "university": self.rng.choice(universities) if kind == "college" and universities else None,
                "tier": self.weighted((("Tier 1", 18), ("Tier 2", 47), ("Tier 3", 35))),
            }
            self.pool.append(entry)
            rows.append([code, name, state, city, f"https://www.{domain}", entry["area"], entry["category"] or "",
                         management, entry["university"] or ""])
            if kind == "university":
                universities.append(name)

        n = 0
        for prefix in prefixes[:40]:
            n += 1
            add("university", n, prefix, self.rng.choice(N.UNIVERSITY_SUFFIXES))
        for i in range(340):
            add("college", i + 1, self.rng.choice(prefixes), self.rng.choice(N.COLLEGE_SUFFIXES))
        for i in range(30):
            add("standalone", i + 1, self.rng.choice(prefixes), self.rng.choice(N.STANDALONE_SUFFIXES))
        frame = pd.DataFrame(rows, columns=["Aishe Code", "Name", "State", "District", "Website", "Location",
                                            "College / Standalone Type", "Management", "University Name"])
        frame.index = range(2, 2 + len(frame))
        report = run_import(self.s, frame, ImportOptions(kind="colleges", list_name="Demo prospect list",
                                                         file_name="demo_prospect_list.xlsx"), self.neha,
                            dry_run=False)
        self.s.flush()
        self.by_code = {e["code"]: e for e in self.pool}
        self.org_by_code = {o.aishe_code: o for o in self.s.scalars(select(Organisation)
                                                                     .where(Organisation.aishe_code.is_not(None)))}
        for org in self.org_by_code.values():
            entry = self.by_code.get(org.aishe_code)
            if entry:
                org.tier = entry["tier"]
        self.free_codes = [e["code"] for e in self.pool]
        self.rng.shuffle(self.free_codes)
        # Keep universities and colleges mixed, but put a few universities near the front: they buy bigger labs.
        self.counts["pool"] = report.rows_imported if hasattr(report, "rows_imported") else len(rows)

    def take_institutions(self, n: int, *, kind: str | None = None) -> list[dict]:
        picked = []
        for code in list(self.free_codes):
            entry = self.by_code[code]
            if kind and entry["kind"] != kind:
                continue
            picked.append(entry)
            self.free_codes.remove(code)
            if len(picked) == n:
                break
        return picked

    # ================================================================== companies and public bodies

    _TYPE_LABEL = {"company": "Company", "startup": "Startup", "government": "Government body",
                   "non_profit": "Non-profit"}

    def _enterprise_pool(self, day: date) -> None:
        """About 60 companies, startups and public bodies, imported as an organisation list with an Industry
        column. A third become leads in the Enterprise & public sector track over the twelve weeks."""
        from app.services.importing.runner import ImportOptions, run_import

        self.at(day, 16)
        industries = list(N.COMPANY_INDUSTRIES)
        weights = [w for _, w in N.COMPANY_INDUSTRIES.values()]
        prefixes = list(N.COMPANY_PREFIXES)
        self.rng.shuffle(prefixes)
        states = ("Karnataka", "Telangana", "Maharashtra", "Tamil Nadu", "Delhi", "Haryana", "Gujarat", "Kerala")
        entries, names = [], set()

        def add(name: str, kind: str, industry: str) -> None:
            if name in names:
                return
            names.add(name)
            state = self.rng.choice(states)
            entries.append({"code": None, "name": name, "kind": kind, "industry": industry, "state": state,
                            "city": self.rng.choice(N.STATES[state]), "domain": f"{_slug(name)}.example",
                            "tier": "", "university": None})

        for prefix in prefixes:
            for _ in range(2):
                industry = self.rng.choices(industries, weights=weights)[0]
                suffix = self.rng.choice(N.COMPANY_INDUSTRIES[industry][0])
                add(f"{prefix} {suffix}", "company", industry)
        for prefix in prefixes[:4]:
            add(f"{prefix} {self.rng.choice(N.STARTUP_SUFFIXES)}", "startup", "IT & software")
        for name, kind, industry in N.PUBLIC_BODIES:
            add(name, kind, industry)
        entries = entries[:54] + [e for e in entries if e["kind"] != "company"]
        frame = pd.DataFrame(
            [[self._TYPE_LABEL[e["kind"]], e["name"], e["industry"], f"https://www.{e['domain']}", e["state"],
              e["city"]] for e in entries],
            columns=["Type", "Name", "Industry", "Website", "State", "District"])
        frame.index = range(2, 2 + len(frame))
        run_import(self.s, frame, ImportOptions(kind="colleges", list_name="Enterprise prospect list",
                                                file_name="enterprise_prospects.xlsx"), self.neha, dry_run=False)
        self.s.flush()
        self.ent_pool = entries
        # A couple of startups and public bodies go first, so the track is more than IT companies from day one.
        startups = [e for e in entries if e["kind"] == "startup"]
        public = [e for e in entries if e["kind"] in ("government", "non_profit")]
        companies = [e for e in entries if e["kind"] == "company"]
        self.rng.shuffle(companies)
        # A telecom company in the first batch: it becomes the track's scripted win (see _first_imports).
        telecom = next((e for e in companies if e["industry"] == "Telecom"), None)
        if telecom is not None:
            companies.remove(telecom)
            companies.insert(0, telecom)
        self.ent_free = startups[:2] + public[:3] + companies

    def take_enterprises(self, n: int) -> list[dict]:
        picked, self.ent_free = self.ent_free[:n], self.ent_free[n:]
        return picked

    def business_rows(self, entries: list[dict], owner: User) -> list[list[str]]:
        rows = []
        for entry in entries:
            for _ in range(self.rng.choice((1, 1, 2))):
                first, last = self.person()
                ctype, designations, _ = self.rng.choices(N.BUSINESS_DESIGNATIONS,
                                                          weights=[d[2] for d in N.BUSINESS_DESIGNATIONS])[0]
                industry = entry["industry"]
                fit = ["NetSim Q", "QSim Cluster"] if industry in ("Telecom", "Banking & finance",
                                                                   "Defence & aerospace") else ["QSim Cluster"]
                rows.append([
                    owner.name, "New", "", "", f"{entry['city']}, {entry['state']}", self._TYPE_LABEL[entry["kind"]],
                    entry["name"], industry, f"{first} {last}", self.email_for(first, last, entry["domain"]),
                    self.phone(), ctype, self.rng.choice(designations), N.BUSINESS_DEPARTMENT[ctype], "", fit[0],
                    fit[1] if len(fit) > 1 else "", "", f"https://www.{entry['domain']}", "",
                ])
        return rows

    def is_business(self, lead: Lead) -> bool:
        return lead.track.code == "enterprise" or lead.organisation.org_type in ("company", "startup", "government")

    def words(self, lead: Lead, academic, business):
        """Picks from the academic or the business wording, so a bank's timeline never mentions the HoD."""
        return self.rng.choice(business if self.is_business(lead) else academic)

    # ================================================================== tracker rows (contacts + product fit)

    def _fit_for(self, department: str) -> list[str]:
        options = {
            "Physics": ["Quantum Sensing Kit", "Quantum Memory Kit", "Entangled-Photon QKD Kit", "QSim Cluster"],
            "ECE": ["NetSim Q", "Entangled-Photon QKD Kit", "QSim Cluster"],
            "CSE": ["QSim Cluster", "NetSim Q"],
            "IT": ["QSim Cluster", "NetSim Q"],
            "EEE": ["Quantum Sensing Kit", "QSim Cluster"],
        }.get(department, ["QSim Cluster", "Quantum Awareness"])
        return self.rng.sample(options, k=min(len(options), self.rng.choice((1, 1, 2))))

    def tracker_rows(self, entries: list[dict], owner: User, *, funding: bool = False) -> list[list[str]]:
        rows = []
        for entry in entries:
            for _ in range(self.rng.choice((1, 1, 2, 2, 3))):
                first, last = self.person()
                ctype, designations, _ = self.rng.choices(N.DESIGNATIONS, weights=[d[2] for d in N.DESIGNATIONS])[0]
                department = self.weighted(N.DEPARTMENTS) if ctype not in ("Registrar", "Purchase/Admin",
                                                                           "Director/Principal") else \
                    ("Administration" if ctype != "Director/Principal" else "")
                title = "Dr. " if ctype in ("Faculty", "HoD", "Dean", "Director/Principal") and self.chance(0.55) else ""
                research = self.rng.choice(N.RESEARCH.get(department, ("",))) if ctype in ("Faculty", "HoD") else ""
                fit = self._fit_for(department or "Physics")
                rows.append([
                    owner.name, "New", entry["tier"], "", f"{entry['city']}, {entry['state']}",
                    entry["kind"].title(), entry["name"], "", f"{title}{first} {last}",
                    self.email_for(first, last, entry["domain"]), self.phone(), ctype,
                    self.rng.choice(designations), department, research, fit[0], fit[1] if len(fit) > 1 else "",
                    entry["code"], f"https://www.{entry['domain']}",
                    ("UG Lab (NQM call)" if self.chance(0.45) else "Institute funds") if funding else "",
                ])
        return rows

    TRACKER_COLUMNS = ["Owner", "Status", "Tier", "Potential", "Location", "Type", "Organisation Name", "Industry",
                       "Contact Name", "Email Id", "Contact Number", "Contact Type", "Designation", "Department",
                       "Faculty Research Interests", "Product Fit", "Product Fit 2", "AISHE Code", "Website",
                       "Funding Source"]

    def import_tracker(self, rows: list[list[str]], *, track: str, source: str, user: User, file_name: str,
                       collected: str = "Organisation website / public directory") -> list[Lead]:
        from app.services.importing.runner import ImportOptions, run_import

        frame = pd.DataFrame(rows, columns=self.TRACKER_COLUMNS)
        frame.index = range(2, 2 + len(frame))
        before = set(self.s.scalars(select(Lead.id)))
        report = run_import(self.s, frame, ImportOptions(kind="tracker", track_id=self.track(track).id,
                                                         lead_source=source, file_name=file_name,
                                                         collection_method=collected, default_owner_id=user.id),
                            user, dry_run=False)
        self.s.flush()
        errors = [i for i in report.issues if i.level == "error"]
        if errors:
            self.log("Import issues:", [e.message for e in errors][:3])
        leads = list(self.s.scalars(select(Lead).where(Lead.id.not_in(before)))) if before else \
            list(self.s.scalars(select(Lead)))
        for lead in leads:
            self.warmth[lead.id] = self.rng.betavariate(3, 4) if track == "referrals" else \
                self.rng.betavariate(3.4, 3.4) if track == "enterprise" else self.rng.betavariate(2, 5)
        return leads

    # ================================================================== a little history: last year's clients

    def _history_clients(self) -> None:
        """Three clients from before the twelve weeks, so warranty, AMC, reference and upsell screens have data."""
        specs = (
            # (days before today: PO, accepted), products, track, partner
            ("Kalpavriksha Institute of Technology", 395, 383, ["QSim Cluster", "NetSim Q"], "cold_colleges", None),
            ("Swarnarekha University", 345, 305, ["QSim Cluster", "Quantum Sensing Kit"], "referrals", None),
            ("Vihanga College of Engineering", 150, 54, ["Entangled-Photon QKD Kit"], "cold_colleges", "northwind"),
        )
        from app.services.procurement import DealData, QuoteDraft, accept_quote, create_deal, create_quote, \
            lines_from_price_items, mark_quote_sent
        from app.services.postsale import save_installation

        self.history_installs = []
        for name, po_ago, accepted_ago, products, track, partner in specs:
            po_day = self.today - timedelta(days=po_ago)
            sent_day = po_day - timedelta(days=21)
            start = sent_day - timedelta(days=40)
            self.at(start, 11)
            entry = next((e for e in self.pool if e["name"] == name), None)
            if entry is None:
                entry = self.take_institutions(1, kind="university" if "University" in name else "college")[0]
                org = self.org_by_code[entry["code"]]
                org.name = name
                entry["name"] = name
            else:
                self.free_codes.remove(entry["code"])
            entry["domain"] = f"{_slug(name)}.example"
            lead = self.import_tracker(self.tracker_rows([entry], self.riya), track=track, source="Earlier outreach",
                                       user=self.riya, file_name="clients_2025.xlsx")[0]
            org = lead.organisation
            org.website, org.domain = f"https://www.{entry['domain']}", entry["domain"]
            if partner:
                lead.partner_id, lead.channel = self.partners[partner].id, "Reseller"
            contact = self.s.scalar(select(Contact).where(Contact.org_id == org.id).order_by(Contact.id))
            contact.verified, contact.verified_on = True, start
            lead.primary_contact_id = contact.id
            self.at(sent_day - timedelta(days=12), 15)
            C.change_stage(self.s, lead, self.stage("Online Demonstration"), self.riya, why="demo held")
            self.at(sent_day, 12)
            basis = "partner" if partner else "institute"
            deal = create_deal(self.s, lead.id, DealData(title=f"{org.name} — starter lab",
                                                         products=[(self.product(p).id, 1) for p in products],
                                                         funding_source="Institute funds",
                                                         buying_route="Direct purchase order"), self.riya)
            picks = [(self.price_items[p].id, 1) for p in products] + [
                (self.price_items["Installation and commissioning"].id, 1)]
            quote = create_quote(self.s, deal.id, QuoteDraft(lines=lines_from_price_items(self.s, picks, basis),
                                                             price_basis=basis), self.riya, on=sent_day)
            mark_quote_sent(self.s, quote.id, self.riya, on=sent_day)
            self.at(po_day, 12)
            accept_quote(self.s, quote.id, po_day, self.riya)
            lead.potential = "100%"
            inst = self.s.scalar(select(Installation).where(Installation.opportunity_id == deal.id))
            accepted = self.today - timedelta(days=accepted_ago)
            delivered = accepted - timedelta(days=6)
            self.at(accepted + timedelta(days=1), 16)
            values = {"delivered_on": delivered, "installed_on": delivered + timedelta(days=3),
                      "accepted_on": accepted, "training_on": accepted + timedelta(days=5), "training_done": True,
                      "reference_ok": True, "case_study_ok": name.startswith("Kalpa")}
            if name.startswith("Vihanga"):
                values |= {"next_phase": "Quantum Memory Kit add-on", "upsell_on": self.today + timedelta(days=9)}
            if name.startswith("Swarnarekha"):
                values |= {"amc_status": "quoted", "next_phase": "Quantum communication add-on"}
            save_installation(self.s, inst.id, values, self.riya)
            # Delivery reminders planned at the PO are long past: tick them off as the team would have.
            for task in self.s.scalars(select(Activity).where(Activity.lead_id == lead.id,
                                                              Activity.status == "planned")):
                if ist_date(task.scheduled_at) < self.first:
                    C.complete_activity(self.s, task, self.riya, outcome="Delivered",
                                        when=at_ist(ist_date(task.scheduled_at), time(16, 0)))
            self.history_installs.append(inst.id)
            self.warmth[lead.id] = 0.9
        # The first client renewed straight into an AMC when the warranty ended.
        kalpa = self.s.get(Installation, self.history_installs[0])
        self.at(kalpa.warranty_end, 12)
        from app.services.postsale import suggested_amc

        amc = suggested_amc(self.s, kalpa.opportunity_id)
        value = amc[0]["Amount"] if amc and "Amount" in amc[0] else Decimal("120000")
        save_installation(self.s, kalpa.id, {"amc_status": "active", "amc_start": kalpa.warranty_end,
                                             "amc_end": kalpa.warranty_end + timedelta(days=365),
                                             "amc_value_inr": str(value)}, self.riya)
        self.s.flush()

    # ================================================================== research institutes, EdTech platforms, schools

    def _other_tracks(self) -> None:
        self.at(self.first - timedelta(days=3), 11)
        specs = (
            ("research_labs", "research_institute", ("Meghavarna Institute for Photonics Research",
                                                     "Kaushambi Centre for Applied Optics",
                                     "Trivenika Advanced Materials Laboratory", "Suryodaya Space Science Institute")),
            ("edtech", "company", ("Learnloop Academy Online", "Qubitly Learning", "Prismline EdTech",
                                   "Corvane Learning Systems")),
            ("schools", "school", ("Saptaparni Public School", "Indradhanu Senior Secondary School",
                              "Ambarvalley International School", "Hemavati Vidya Mandir", "Rajhans Residential School")),
        )
        roles = {"research_labs": "Lab Director", "edtech": "Partnerships Head", "schools": "Principal"}
        role_types = {"research_labs": "Director/VP", "edtech": "Director/VP", "schools": "Director/Principal"}
        self.channel_leads: list[int] = []
        for code, org_type, orgs in specs:
            for i, name in enumerate(orgs):
                state = self.rng.choices(list(N.STATE_WEIGHTS), weights=list(N.STATE_WEIGHTS.values()))[0]
                domain = f"{_slug(name)}.example"
                industry = {"research_labs": "Research & development", "edtech": "EdTech",
                            "schools": "School education"}[code]
                org = Organisation(name=name, org_type=org_type, industry=industry, state=state,
                                   city=self.rng.choice(N.STATES[state]), website=f"https://www.{domain}",
                                   domain=domain)
                self.s.add(org)
                self.s.flush()
                first, last = self.person()
                contact = Contact(org_id=org.id, name=f"{first} {last}", designation=roles[code],
                                  contact_type=role_types[code], email=self.email_for(first, last, domain),
                                  phone=self.phone(),
                                  verified=True, verified_on=self.first - timedelta(days=3),
                                  verified_by_user_id=self.riya.id, source="Company website")
                self.s.add(contact)
                self.s.flush()
                lead = Lead(org_id=org.id, track_id=self.track(code).id, stage=self.stage("New"),
                            primary_contact_id=contact.id, owner_user_id=self.riya.id,
                            source={"research_labs": "Research institutes", "edtech": "EdTech platforms",
                                    "schools": "Schools"}[code],
                            sub_tier=("Senior secondary" if i < 3 else "Secondary") if code == "schools" else None)
                self.s.add(lead)
                self.s.flush()
                self.warmth[lead.id] = self.rng.betavariate(3, 3)
                self.channel_leads.append(lead.id)
        self.s.flush()

    # ================================================================== one day

    def _day(self, d: date) -> None:
        self.at(d, 8, 50)
        from app.services.partners import refresh_registrations
        from app.services.postsale import sync_installations
        from app.services.procurement import expire_quotes

        C.run_daily_checks(self.s, today=d)
        expire_quotes(self.s, today=d)
        refresh_registrations(self.s, today=d)
        sync_installations(self.s)
        if d.weekday() == 0:
            self._monday(d)
        if d == self.today:
            self._this_morning(d)
            self.s.flush()
            return
        if d.weekday() == 6:
            self._agenda(d, inbound_only=True)
            self._one_offs(d)
            self.s.flush()
            return
        self._ghost_decisions(d)
        self._lead_generation(d)
        self._verification(d)
        self._manual_starts(d)
        self._queries(d)
        self._work_tasks(d)
        self._agenda(d)
        self._signals(d)
        self._content(d)
        self._one_offs(d)
        if d.weekday() == 4:
            self._friday_notes(d)
        self.s.flush()

    def _one_offs(self, d: date) -> None:
        """Things that happen on a particular date, weekend or not."""
        self._partner_deals(d)
        self._events(d)
        self._privacy(d)
        self._odd_jobs(d)

    # ---------------------------------------------------------------- leads coming in

    def _first_imports(self, d: date) -> None:
        fresh = self.take_institutions(24)
        self.at(d, 7, 5)
        self.import_tracker(self.tracker_rows(fresh, self.neha), track="cold_colleges", source="Fresh",
                            user=self.neha, file_name="fresh_leads_batch_01.xlsx")
        warm = self.take_institutions(22)
        self.at(d, 7, 20)
        self.import_tracker(self.tracker_rows(warm, self.kabir, funding=True), track="referrals",
                            source="Referral", user=self.kabir, file_name="referrals_and_past_contacts.xlsx",
                            collected="Referral / partner")
        self.at(d, 7, 40)
        leads = self.import_tracker(self.business_rows(self.take_enterprises(12), self.kabir), track="enterprise",
                                    source="Enterprise prospect list", user=self.kabir,
                                    file_name="enterprise_targets.xlsx")
        # One telecom company goes all the way: reply → brochure → demo → quote → PO.
        star = next((lead for lead in leads if lead.organisation.industry == "Telecom"), leads[0])
        self.warmth[star.id] = 0.9
        self.plan(d + timedelta(days=9), star.id, "reply_win")

    def _do_reply_win(self, lead: Lead, d: date) -> None:
        self._do_reply(lead, d, scripted=True)
        self.plan(d + timedelta(days=2), lead.id, "brochure_win")

    def _lead_generation(self, d: date) -> None:
        if d == self.first:
            return
        if d.weekday() in (1, 3):
            batch = self.take_institutions(self.rng.randint(5, 7))
            if batch:
                self.at(d, 11, self.rng.randint(0, 50))
                number = (d - self.first).days // 2 + 1
                self.import_tracker(self.tracker_rows(batch, self.neha), track="cold_colleges", source="Fresh",
                                    user=self.neha, file_name=f"research_batch_{number:02d}.xlsx")
        if d.weekday() == 3 and self.ent_free:
            batch = self.take_enterprises(self.rng.randint(2, 3))
            self.at(d, 15, self.rng.randint(0, 50))
            self.import_tracker(self.business_rows(batch, self.kabir), track="enterprise",
                                source="Enterprise prospect list", user=self.neha,
                                file_name=f"enterprise_batch_{(d - self.first).days // 7 + 1:02d}.xlsx")
        if d.weekday() == 2 and (d - self.first).days % 14 < 7:
            batch = self.take_institutions(2)
            if batch:
                self.at(d, 12, 30)
                self.import_tracker(self.tracker_rows(batch, self.kabir, funding=True), track="referrals",
                                    source="Referral", user=self.kabir, file_name="referrals_new.xlsx",
                                    collected="Referral / partner")

    def _verification(self, d: date) -> None:
        """Neha verifies fresh contacts; Kabir verifies warm ones. A few stay unverified (the data-quality view)."""
        for track, user, per_day in (("cold_colleges", self.neha, self.rng.randint(4, 7)),
                                     ("referrals", self.kabir, self.rng.randint(1, 3)),
                                     ("enterprise", self.kabir, self.rng.randint(1, 3))):
            if d >= self.today - timedelta(days=2):
                per_day = max(per_day - 3, 0)
            leads = list(self.s.scalars(
                select(Lead).where(Lead.track_id == self.track(track).id, Lead.cadence_status.is_(None),
                                   Lead.stage_id == self.stage("New").id)
                .where(select(Contact.id).where(Contact.org_id == Lead.org_id, Contact.verified.is_(False))
                       .exists()).order_by(Lead.id).limit(per_day)))
            for lead in leads:
                self.at(d, self.rng.randint(10, 13), self.rng.randint(0, 59))
                for contact in self.s.scalars(select(Contact).where(Contact.org_id == lead.org_id,
                                                                    Contact.verified.is_(False))):
                    if contact.email and self.chance(0.94):
                        contact.verified, contact.verified_on, contact.verified_by_user_id = True, d, user.id
                        contact.verification_note = ("Checked on LinkedIn" if track == "enterprise"
                                                     else "Checked on the department page")
                        self.s.add(AuditLog(user_id=user.id, entity="contacts", entity_id=str(contact.id),
                                            action="update", diff={"Verified": [False, True]}))
        self.s.flush()

    def _manual_starts(self, d: date) -> None:
        """Referrals are started by hand (Mon/Wed); the institute, EdTech and school tracks over the first weeks."""
        if d.weekday() in (0, 2):
            ids = list(self.s.scalars(select(Lead.id).where(
                Lead.track_id == self.track("referrals").id, Lead.cadence_status.is_(None),
                Lead.stage_id == self.stage("New").id).order_by(Lead.id).limit(4)))
            if ids:
                self.at(d, 10, 5)
                C.start_cadence(self.s, ids, self.kabir, on=d)
        if self.channel_leads and d.weekday() in (1, 3):
            batch, self.channel_leads = self.channel_leads[:2], self.channel_leads[2:]
            self.at(d, 10, 15)
            C.start_cadence(self.s, batch, self.riya, on=d)

    def _queries(self, d: date) -> None:
        from app.services.queries import NewQuery, create_query, move_lead_track

        if not self.chance(0.32):
            return
        self.at(d, self.rng.randint(9, 16), self.rng.randint(0, 59))
        channel = self.rng.choice(("Website", "Website", "Email", "WhatsApp", "Phone", "Event"))
        first, last = self.person()
        kind = self.rng.random()
        org_id, new_name, domain = None, None, "mail.example"
        if kind < 0.55:
            entry = self.take_institutions(1)
            if entry:
                org_id, domain = self.org_by_code[entry[0]["code"]].id, entry[0]["domain"]
        elif kind < 0.7:
            new_name = f"{self.rng.choice(N.PREFIXES)} Academy of Sciences"
        product = self.rng.choice(("QSim Mini", "NetSim Mini", "QSim Cluster", "Entangled-Photon QKD Kit"))
        individual = org_id is None and new_name is None
        lead, _ = create_query(self.s, NewQuery(
            channel=channel, received_at=datetime.now(IST), name=f"{first} {last}",
            email=self.email_for(first, last, domain), phone=self.phone(), org_id=org_id, new_org_name=new_name,
            products=[product], message=self.rng.choice(N.ENQUIRIES), owner_id=self.arjun.id
            if self.chance(0.4) else self.riya.id), self.riya)
        self.warmth[lead.id] = self.rng.betavariate(5, 3)
        self.counts["queries"] += 1
        if individual and product.endswith("Mini") and self.chance(0.8):
            self.plan(d + timedelta(days=self.rng.randint(2, 4)), lead.id, "to_mini")
        lead.potential = "30-50%"

    # ---------------------------------------------------------------- working the task list

    def _special(self, task: Activity) -> bool:
        notes = task.notes or ""
        if notes.startswith("[post:") or task.title.startswith("Prepare last week's report"):
            return True
        return task.activity_type.name == "Demo" and task.cadence_step_id is None

    def _work_tasks(self, d: date) -> None:
        tasks = list(self.s.scalars(select(Activity).where(
            Activity.status == "planned", Activity.scheduled_at < at_ist(d + timedelta(days=1), time(0, 0)))
            .order_by(Activity.scheduled_at, Activity.id)))
        recent = self.today - timedelta(days=2)
        for task in tasks:
            if task.status != "planned" or self._special(task):
                continue
            due = ist_date(task.scheduled_at)
            reply_to_query = task.cadence_step is not None and task.cadence_step.step_no == 1 and \
                task.lead is not None and task.lead.track_id == self.track("inbound_queries").id
            if due >= recent and self.chance(0.45) and not (reply_to_query and due < self.today - timedelta(days=1)):
                continue  # a few things slip in the last days: Today shows them as overdue
            if d.weekday() == 5 and self.chance(0.6) and not reply_to_query:
                continue  # Saturdays are light; the rest waits for Monday
            owner = self.s.get(User, task.owner_user_id) if task.owner_user_id else self.riya
            when = self.work_time(d)
            lead = task.lead
            channel = task.activity_type.channel
            if task.cadence_step_id is not None:
                if lead is None or self.is_closed(lead):
                    C.skip_activity(self.s, task, owner, "Lead closed")
                    continue
                if channel == "email":
                    roll = 1.0 if reply_to_query else self.rng.random()
                    if roll < 0.025 and task.participants:
                        from app.services.tasks import mark_task_bounced

                        mark_task_bounced(self.s, task.id, owner)
                        continue
                    if roll < 0.06:
                        C.skip_activity(self.s, task, owner, "Sent from the shared inbox instead")
                        continue
                    sent = ("Sent", "Sent with the use-case note", "Sent — personalised for their team")
                    C.complete_activity(self.s, task, owner, when=when,
                                        outcome=self.words(lead, N.EMAIL_OUTCOMES, sent))
                else:
                    if self.chance(0.14):
                        C.skip_activity(self.s, task, owner, "Couldn't reach them")
                        continue
                    C.complete_activity(self.s, task, owner, when=when, notes=None,
                                        outcome=self.words(lead, N.CALL_OUTCOMES, N.BUSINESS_CALL_OUTCOMES))
                self._maybe_reply(lead, d, channel)
                continue
            # Everything else: follow-ups from next steps, delivery reminders, quote reminders, client reminders.
            outcome = "Done"
            notes = task.notes or ""
            if task.title.startswith("Delivery due"):
                outcome = "Delivered and signed for"
                self._mark_delivered(task, d)
            elif notes.startswith("quote:"):
                outcome = "Called the purchase office about the quote"
            elif notes.startswith("[client:"):
                outcome = "Done — noted on the client record"
                if ":training]" in notes.split("\n")[0]:
                    inst = self.s.get(Installation, int(notes.split(":")[1]))
                    inst.training_done = True
            elif channel in ("phone", "whatsapp"):
                outcome = self.rng.choice(N.CALL_OUTCOMES)
            C.complete_activity(self.s, task, owner, outcome=outcome, when=when)
        self.s.flush()

    def _maybe_reply(self, lead: Lead | None, d: date, channel: str) -> None:
        if lead is None or self.is_closed(lead) or lead.cadence_status not in C.IN_CADENCE:
            return
        p = self.warmth.get(lead.id, 0.2) * (0.21 if channel == "email" else 0.17)
        if self.chance(p):
            self.plan(d + timedelta(days=self.rng.randint(1, 4)), lead.id, "reply")

    def _mark_delivered(self, task: Activity, d: date) -> None:
        lead = task.lead
        if lead is None:
            return
        deal = self.s.scalar(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.status == "won")
                             .order_by(Opportunity.id.desc()))
        if deal is None:
            return
        for item in deal.line_items:
            if item.delivered_on is None and task.title.startswith(f"Delivery due: {item.product.name}"):
                item.delivered_on = d
        inst = self.s.scalar(select(Installation).where(Installation.opportunity_id == deal.id))
        if inst is not None and inst.delivered_on is None:
            inst.delivered_on = d
            self.plan(d + timedelta(days=self.rng.randint(2, 4)), lead.id, "install")

    # ---------------------------------------------------------------- the conversation agenda

    def _agenda(self, d: date, *, inbound_only: bool = False) -> None:
        due = sorted((item for item in self.agenda if item[0] <= d), key=lambda x: (x[0], x[1]))
        if inbound_only:
            due = [item for item in due if item[3] == "reply"]
        for item in due:
            self.agenda.remove(item)
            _, _, lead_id, action = item
            lead = self.lead_of(lead_id)
            if lead is None or (self.is_closed(lead) and action not in ("install",)):
                continue
            getattr(self, f"_do_{action}")(lead, d)
        self.s.flush()

    def _contact(self, lead: Lead) -> Contact | None:
        contact = self.s.get(Contact, lead.primary_contact_id) if lead.primary_contact_id else None
        return contact or self.s.scalar(select(Contact).where(Contact.org_id == lead.org_id).order_by(Contact.id))

    def _log(self, lead: Lead, d: date, type_name: str, user: User, *, direction: str = "outbound",
             notes: str | None = None, outcome: str | None = None, title: str | None = None,
             documents: list[int] | None = None, next_step: NextStep | None = None, hour: int | None = None):
        contact = self._contact(lead)
        when = self.at(d, hour or self.rng.randint(10, 17), self.rng.choice((5, 15, 25, 35, 45)))
        return log_activity(self.s, LogEntry(
            org_id=lead.org_id, lead_id=lead.id, type_name=type_name, when=when, direction=direction,
            notes=notes, outcome=outcome, title=title, contact_ids=[contact.id] if contact else [],
            document_ids=documents or [], next_step=next_step,
            duration_min={"Phone call": 12, "WhatsApp call": 10, "Meeting (online)": 45, "Demo": 60}.get(type_name)),
            user)

    def _do_reply(self, lead: Lead, d: date, *, scripted: bool = False) -> None:
        kind = "WhatsApp message" if self.chance(0.25) and not self.is_business(lead) else "Email"
        self._log(lead, d, kind, self.owner_of(lead), direction="inbound",
                  notes=self.words(lead, N.REPLIES, N.BUSINESS_REPLIES),
                  title=f"{kind} from {(self._contact(lead).name if self._contact(lead) else lead.organisation.name)}",
                  hour=self.rng.randint(9, 18))
        lead.potential = lead.potential or "30-50%"
        self.counts["replies"] += 1
        self.plan(d + timedelta(days=self.rng.choice((0, 1, 1))), lead.id, "respond")
        if scripted:
            return
        roll = self.rng.random()
        if roll < 0.74:
            self.plan(d + timedelta(days=self.rng.randint(1, 3)), lead.id, "brochure")
        elif roll < 0.86:
            self.plan(d + timedelta(days=self.rng.randint(6, 15)), lead.id, "reject")

    def _do_respond(self, lead: Lead, d: date) -> None:
        self._log(lead, d, "Email", self.riya, outcome="Answered their questions", notes="Replied with details.")

    def _brochure_for(self, lead: Lead) -> list[int]:
        names = {p.name for p in lead.product_fit}
        docs = []
        if names & {"QSim Cluster", "QSim Mini"} or not names:
            docs.append(self.docs["brochure_qsim"].id)
        if names & {"NetSim Q", "NetSim Mini"}:
            docs.append(self.docs["brochure_netsim"].id)
        if names & {"Quantum Sensing Kit", "Entangled-Photon QKD Kit", "Quantum Memory Kit"}:
            docs.append(self.docs["brochure_kits"].id)
        return docs

    def _do_brochure(self, lead: Lead, d: date) -> None:
        self._log(lead, d, "Email", self.riya, outcome="Sent the brochure", documents=self._brochure_for(lead),
                  title=f"Brochure sent — {lead.organisation.name}",
                  next_step=NextStep("Phone call", d + timedelta(days=3), owner_id=self.riya.id,
                                     title=f"Call about the brochure — {lead.organisation.name}"))
        roll = self.rng.random()
        if roll < 0.62:
            self.plan(d + timedelta(days=self.rng.randint(3, 7)), lead.id, "book_demo")
        elif roll < 0.82:
            self.plan(d + timedelta(days=self.rng.randint(15, 25)), lead.id, "go_quiet")
        else:
            self.plan(d + timedelta(days=self.rng.randint(8, 18)), lead.id, "reject")

    def _do_book_demo(self, lead: Lead, d: date) -> None:
        presenter = self.riya if self.chance(0.6) else self.arjun
        demo_day = d + timedelta(days=self.rng.randint(4, 9))
        if demo_day.weekday() == 6:
            demo_day += timedelta(days=1)
        self._log(lead, d, self.rng.choice(("Phone call", "WhatsApp call")), self.riya,
                  outcome="Booked an online demo for the team" if self.is_business(lead)
                  else "Booked an online demo for the department",
                  next_step=NextStep("Demo", demo_day, at=time(self.rng.choice((11, 14, 15)), 0),
                                     owner_id=presenter.id, title=f"Online demo — {lead.organisation.name}"))
        lead.potential = "50-70%"
        self.plan(demo_day, lead.id, "demo_done")

    def _do_demo_done(self, lead: Lead, d: date) -> None:
        demo = self.s.scalar(select(Activity).where(Activity.lead_id == lead.id, Activity.status == "planned",
                                                    Activity.type_id == self._type_id("Demo"))
                             .order_by(Activity.scheduled_at))
        if demo is None:
            return
        if ist_date(demo.scheduled_at) > d:
            self.plan(ist_date(demo.scheduled_at), lead.id, "demo_done")
            return
        if d >= self.today:
            return
        self.at(d, demo.scheduled_at.astimezone(IST).hour, 0)
        presenter = self.s.get(User, demo.owner_user_id)
        finish_task(self.s, demo.id, presenter, outcome=self.words(lead, (
            "Demo done — HoD and 6 faculty attended; asked for a quote",
            "Demo done — strong interest from final-year project guides",
            "Demo done — questions on server requirements",
            "Demo done — principal joined for the last 15 minutes"), N.BUSINESS_DEMO_OUTCOMES))
        C.change_stage(self.s, lead, self.stage("Online Demonstration"), presenter, why="demo held")
        self.counts["demos"] += 1
        roll = self.rng.random()
        if roll < 0.25:
            self.plan(d + timedelta(days=self.rng.randint(6, 12)), lead.id, "offline_demo")
        elif roll < 0.85:
            self.plan(d + timedelta(days=self.rng.randint(3, 8)), lead.id, "quote")
        else:
            self.plan(d + timedelta(days=self.rng.randint(10, 20)), lead.id, "reject")

    def _do_offline_demo(self, lead: Lead, d: date) -> None:
        business = self.is_business(lead)
        self._log(lead, d, "Meeting (in person)", self.arjun,
                  outcome="Workshop at their office with the engineering team" if business
                  else "On-campus demonstration with the kit team",
                  title=f"{'Office' if business else 'Campus'} visit and demo — {lead.organisation.name}")
        C.change_stage(self.s, lead, self.stage("Offline Demonstration"), self.arjun, why="campus demo")
        self.plan(d + timedelta(days=self.rng.randint(3, 7)), lead.id, "quote")

    def _do_quote(self, lead: Lead, d: date, *, force_win: bool = False) -> None:
        from app.services.procurement import DealData, QuoteDraft, create_deal, create_quote, lines_from_bundle, \
            lines_from_price_items, mark_quote_sent, suggest_route

        self.at(d, 12, 10)
        fit = [p.name for p in lead.product_fit] or ["QSim Cluster"]
        kits = [p for p in fit if p.endswith("Kit")]
        basis = "partner" if lead.partner_id else "institute"
        if kits:
            picks = [(self.price_items[k].id, 1) for k in kits[:2]] + [
                (self.price_items["Installation and commissioning"].id, 1)]
            lines = lines_from_price_items(self.s, picks, basis)
            title = f"{lead.organisation.name} — experimentation kits"
        elif "QSim Mini" in fit or "NetSim Mini" in fit:
            mini = "QSim Mini" if "QSim Mini" in fit else "NetSim Mini"
            lines = lines_from_price_items(self.s, [(self.price_items[mini].id, self.rng.randint(1, 3))], basis)
            title = f"{lead.organisation.name} — {mini}"
        elif self.is_business(lead):
            picks = [(self.price_items["QSim Cluster"].id, 1),
                     (self.price_items["Team upskilling workshop (2 days)"].id, 1)]
            if "NetSim Q" in fit:
                picks.insert(1, (self.price_items["NetSim Q"].id, 1))
            lines = lines_from_price_items(self.s, picks, basis)
            title = f"{lead.organisation.name} — quantum upskilling pilot"
        else:
            lines = lines_from_bundle(self.s, self.bundles["phase1"].id, basis)
            title = f"{lead.organisation.name} — starter simulation lab"
        close = d + timedelta(days=self.rng.randint(20, 50))
        route = suggest_route(lead.organisation.management) or "Direct purchase order"
        deal = create_deal(self.s, lead.id, DealData(
            title=title, products=[(self.product(n).id, 1) for n in fit if n in self._products],
            funding_source="NQM UG teaching lab grant" if lead.nqm_flag else
            "Company training budget" if self.is_business(lead) else "Institute funds",
            buying_route=route, expected_close=close), self.riya)
        quote = create_quote(self.s, deal.id, QuoteDraft(
            lines=lines, price_basis=basis, discount_pct=Decimal(self.rng.choice((0, 0, 5, 10)))), self.riya, on=d)
        self.at(d, 16, 30)
        mark_quote_sent(self.s, quote.id, self.riya, on=d)
        lead.potential = "70-90%"
        self.quotes_by_lead[lead.id] = quote.id
        self.counts["quotes"] += 1
        roll = 0.0 if force_win else self.rng.random()
        if roll < 0.38:
            self.plan(d + timedelta(days=self.rng.randint(9, 24)), lead.id, "po")
        elif roll < 0.6:
            self.plan(d + timedelta(days=self.rng.randint(14, 24)), lead.id, "reject")
        # else: the quote stays open (and may expire) — the Quotes page shows days left

    def _do_po(self, lead: Lead, d: date) -> None:
        from app.services.procurement import accept_quote

        quote = self.s.get(Quote, self.quotes_by_lead.get(lead.id))
        if quote is None or quote.status not in ("sent", "expired"):
            return
        self.at(d, 11, 40)
        accept_quote(self.s, quote.id, d, self.riya)
        lead.potential = "100%"
        self.counts["won"] += 1
        self._log(lead, d, "Email", self.riya, outcome="Thanked them for the PO; shared the delivery plan",
                  title=f"PO received — {lead.organisation.name}")

    def _do_install(self, lead: Lead, d: date) -> None:
        from app.services.postsale import save_installation

        deal = self.s.scalar(select(Opportunity).where(Opportunity.lead_id == lead.id, Opportunity.status == "won")
                             .order_by(Opportunity.id.desc()))
        inst = self.s.scalar(select(Installation).where(Installation.opportunity_id == deal.id)) if deal else None
        if inst is None or inst.installed_on is not None or d >= self.today:
            return
        self.at(d, 17, 0)
        accepted = d + timedelta(days=2)
        values = {"installed_on": d, "accepted_on": accepted if accepted < self.today else None,
                  "training_on": d + timedelta(days=self.rng.randint(5, 12))}
        save_installation(self.s, inst.id, values, self.riya)

    def _do_reject(self, lead: Lead, d: date) -> None:
        from app.models import LostReason

        notes = N.BUSINESS_LOST_NOTES if self.is_business(lead) else N.LOST_NOTES
        reason = self.rng.choice(list(notes))
        lost = self.s.scalar(select(LostReason).where(LostReason.name == reason))
        self.at(d, 15, 20)
        lead.lost_reason_id, lead.lost_note, lead.potential = lost.id if lost else None, notes[reason], "0%"
        C.change_stage(self.s, lead, self.stage("Rejected"), self.riya, why=reason.lower())
        C.stop_if_conversation_moved(self.s, lead, self.riya)
        for task in self.s.scalars(select(Activity).where(Activity.lead_id == lead.id, Activity.status == "planned")):
            task.status, task.outcome = "cancelled", "Lead rejected"
        quote = self.s.get(Quote, self.quotes_by_lead.get(lead.id)) if lead.id in self.quotes_by_lead else None
        if quote is not None and quote.status == "sent":
            from app.services.procurement import cancel_quote

            cancel_quote(self.s, quote.id, self.riya)
        for deal in self.s.scalars(select(Opportunity).where(Opportunity.lead_id == lead.id,
                                                             Opportunity.status == "open")):
            deal.status = "lost"
        self.counts["rejected"] += 1

    def _do_go_quiet(self, lead: Lead, d: date) -> None:
        if lead.stage.outcome == "open" and lead.stage.name in ("Replied to Queries", "Sent Brochure"):
            self.at(d, 16, 0)
            C.mark_ghosted(self.s, lead, self.riya)

    def _do_to_mini(self, lead: Lead, d: date) -> None:
        from app.services.queries import move_lead_track

        if lead.track.code != "inbound_queries":
            return
        self.at(d, 12, 0)
        move_lead_track(self.s, lead.id, self.track("individuals").id, self.riya)
        if self.chance(0.6):
            self.plan(d + timedelta(days=self.rng.randint(4, 10)), lead.id, "quote")

    def _type_id(self, name: str) -> int:
        from app.models import ActivityType

        if not hasattr(self, "_types"):
            self._types = {t.name: t.id for t in self.s.scalars(select(ActivityType))}
        return self._types[name]

    # ---------------------------------------------------------------- daily decisions

    def _ghost_decisions(self, d: date) -> None:
        """Riya decides on cadences that finished without a reply — except the last couple of days' (left for Today)."""
        finished = list(self.s.scalars(select(Lead).where(Lead.cadence_status == "finished",
                                                          Lead.cadence_due_on <= d - timedelta(days=2))))
        for lead in finished:
            if d >= self.today - timedelta(days=3):
                break
            self.at(d, 9, 30)
            if self.chance(0.85):
                C.mark_ghosted(self.s, lead, self.riya)
            else:
                C.keep_open(self.s, lead, self.riya)

    def _monday(self, d: date) -> None:
        from app.services.report_metrics import (
            TARGET_METRICS,
            ensure_monday_task,
            finalise,
            save_comments,
            save_targets,
            targets_frame,
        )

        if d < self.first + timedelta(days=1):
            return
        ensure_monday_task(self.s, today=d)
        if d == self.today:
            return  # this week's report and targets are still to do: the task waits on Riya's Today
        self.at(d, 10, 30)
        frame, _ = targets_frame(self.s, d)
        plan = {
            "Kabir Rao": {"outreach": 30, "followup_emails": 45, "contacts_verified": 8, "replies": 4},
            "Neha Iyer": {"new_leads": 7, "contacts_verified": 25},
            "Riya Sharma": {"followup_calls": 25, "demos": 2, "quotes": 1, "replies": 3},
            "Arjun Menon": {"followup_calls": 10, "demos": 1},
        }
        for i, row in frame.iterrows():
            for key, label in TARGET_METRICS.items():
                value = plan.get(row["Person"], {}).get(key)
                frame.at[i, label] = value
        save_targets(self.s, d, frame, self.riya)
        last_week = d - timedelta(days=7)
        if last_week >= self.first - timedelta(days=7):
            self.at(d, 11, 15)
            save_comments(self.s, last_week, highlights=self.rng.choice((
                "Two demos booked from referrals; referrals reply faster than cold outreach.",
                "First reply from a state skills mission; reseller registered a new institution.",
                "Quote sent for a starter simulation lab; hackathon brought three new institutions.",
                "Email batch running daily; reply rate steady.")),
                risks=self.rng.choice(("Purchase committees asking for a GeM listing — PAC route explained.",
                                       "Budget meetings moved to next month at two institutions.",
                                       "Bounce rate up on one research batch; Neha re-checking the sources.")),
                asks=self.rng.choice(("Leadership intro to two vice chancellors.",
                                      "Approval for the workshop with the training partner.",
                                      "None this week.")), user=self.riya)
            finalise(self.s, last_week, self.riya, today=d)
            for task in self.s.scalars(select(Activity).where(Activity.status == "planned",
                                                              Activity.title.like("Prepare last week's report%"))):
                C.complete_activity(self.s, task, self.riya, outcome="Report finalised; targets set")

    def _friday_notes(self, d: date) -> None:
        from app.services.report_metrics import monday_of, save_note

        notes = {
            self.kabir: ("Email batch sent every day; 3 bounces cleaned.", "Referral verification caught up.",
                         "Two LinkedIn posts out; the hackathon recap did best."),
            self.neha: ("Two research batches imported; verification rate above 90%.",
                        "Found department pages for 9 colleges in the pool.", "Cleaned duplicate contacts in Pune."),
            self.riya: ("Demos this week went well; two quotes likely next week.",
                        "Reseller pipeline growing in the south.", "Followed up on expiring quotes."),
        }
        for user, options in notes.items():
            self.at(d, 17, 30)
            save_note(self.s, monday_of(d), self.rng.choice(options), user)

    # ---------------------------------------------------------------- partners

    def _partner_deals(self, d: date) -> None:
        from app.services.partners import approve_registration, register_deal, reject_registration

        offset = (d - self.first).days
        northwind = self.partners["northwind"]
        if offset in (4, 30):
            entry = self.take_institutions(1, kind="college")[0]
            org = self.org_by_code[entry["code"]]
            self.at(d, 11, 0)
            reg, _ = register_deal(self.s, partner_id=northwind.id, user=self.riya, org_id=org.id,
                                   product_ids=[self.product("QSim Cluster").id],
                                   expected_value="1100000", contact_name="Principal",
                                   notes="Northwind met the principal at a regional expo.")
            self.at(d + timedelta(days=0), 15, 0)
            lead, _ = approve_registration(self.s, reg.id, track_id=self.track("cold_colleges").id, user=self.riya,
                                           note="No overlap; approved.")
            first, last = self.person()
            contact = Contact(org_id=org.id, name=f"Dr. {first} {last}", designation="Principal",
                              contact_type="Director/Principal", email=self.email_for(first, last, entry["domain"]),
                              phone=self.phone(), verified=True, verified_on=d, verified_by_user_id=self.riya.id,
                              source="Partner: Northwind Lab Supplies")
            self.s.add(contact)
            self.s.flush()
            lead.primary_contact_id, lead.potential = contact.id, "30-50%"
            self.warmth[lead.id] = 0.8
            if offset == 4:  # the first one goes all the way to a PO (Northwind's second won sale)
                self.plan(d + timedelta(days=5), lead.id, "brochure_win")
            else:
                self.plan(d + timedelta(days=6), lead.id, "brochure")
        if offset == 52:
            taken = self.s.scalar(select(Lead).where(Lead.track_id == self.track("cold_colleges").id,
                                                     Lead.partner_id.is_(None)).order_by(Lead.id))
            self.at(d, 11, 0)
            reg, overlap = register_deal(self.s, partner_id=northwind.id, user=self.riya, org_id=taken.org_id,
                                         notes="Partner asked to take over this institution.")
            self.at(d, 16, 0)
            reject_registration(self.s, reg.id, user=self.riya, note="Already our direct lead with an open "
                                                                      "conversation.")

    def _do_brochure_win(self, lead: Lead, d: date) -> None:
        self._log(lead, d, "Email", self.riya, outcome="Sent the brochure", documents=self._brochure_for(lead),
                  title=f"Brochure sent — {lead.organisation.name}")
        self.plan(d + timedelta(days=6), lead.id, "demo_win")

    def _do_demo_win(self, lead: Lead, d: date) -> None:
        self._log(lead, d, "Demo", self.arjun, outcome="Demo for the CTO and the network security team"
                  if self.is_business(lead) else "Demo for the principal and CSE faculty",
                  title=f"Online demo — {lead.organisation.name}")
        C.change_stage(self.s, lead, self.stage("Online Demonstration"), self.arjun, why="demo held")
        self.counts["demos"] += 1
        self.plan(d + timedelta(days=5), lead.id, "quote_win")

    def _do_quote_win(self, lead: Lead, d: date) -> None:
        self._do_quote(lead, d, force_win=True)

    # ---------------------------------------------------------------- signals

    SIGNAL_NOTES = {
        "NQM lab approved": "Selected for an NQM undergraduate teaching lab",
        "AICTE quantum minor adopted": "Adopted the B.Tech Minor in Quantum Technologies from this year",
        "New department / centre of excellence": "Announced a centre for quantum technologies",
        "Funding / grant": "Received a state grant for new teaching labs",
        "FDP / workshop": "Hosted a faculty development programme on quantum computing",
        "Tender / RFQ": "Invited quotations for quantum lab equipment",
        "Quantum team or programme announced": "Announced a quantum-safe security programme and is hiring for it",
    }

    def _signals(self, d: date) -> None:
        from app.services.signals import add_signal

        if not self.chance(0.42):
            return
        tracks = (self.track("cold_colleges").id, self.track("referrals").id, self.track("enterprise").id)
        leads = list(self.s.scalars(select(Lead).join(Stage).where(Stage.outcome.in_(("open", "nurture")),
                                                                   Lead.track_id.in_(tracks))))
        if not leads:
            return
        lead = self.rng.choice(leads)
        if self.is_business(lead):
            name = "Tender / RFQ" if lead.organisation.org_type == "government" else \
                "Quantum team or programme announced"
        else:
            name = self.rng.choices(list(self.SIGNAL_NOTES)[:6], weights=(3, 4, 2, 3, 4, 1))[0]
        kind = self._signal_types.get(name)
        if kind is None:
            return
        self.at(d, 12, 45)
        add_signal(self.s, org_id=lead.org_id, type_id=kind.id, observed_on=d - timedelta(days=self.rng.randint(0, 3)),
                   user=self.rng.choice((self.riya, self.kabir, self.neha)), note=self.SIGNAL_NOTES[name])
        # News of a lab or a tender makes them easier to talk to.
        self.warmth[lead.id] = min(0.95, self.warmth.get(lead.id, 0.2) + 0.25)
        self.counts["signals"] += 1

    # ---------------------------------------------------------------- content calendar

    POSTS = (
        ("Why start with simulators?", "Product",
         "Most quantum programmes stall waiting for lab space. A simulation lab is ready for teaching in about two "
         "weeks on an existing server — students write and run circuits from day one, and benchtop kits can follow "
         "later.\n\nHow is your department planning its first quantum course?"),
        ("Hackathon recap", "Event",
         "36 students, 11 teams, 24 hours. The winning team modelled an entanglement-distribution network for a "
         "smart campus. Thank you to every mentor and judge who joined us!"),
        ("What the NQM teaching-lab call means for colleges", "NQM & policy",
         "The National Quantum Mission's undergraduate teaching-lab call is open to AICTE-affiliated institutions that "
         "have adopted the quantum minor. Here's a plain-language checklist for heads of department."),
        ("Careers in quantum — beyond research", "Hiring & careers",
         "Quantum teams hire software engineers, test engineers and lab technicians as much as physicists. Three "
         "skills students can build in their second year."),
        ("A student project we loved", "Student project",
         "A final-year team measured magnetic fields with rubidium vapour and compared their results with a "
         "smartphone magnetometer. Simple setup, beautiful physics."),
        ("Behind the scenes: packing a kit", "Behind the scenes",
         "Every experimentation kit is aligned and tested before it ships. A day with our build team."),
        ("BBM92 in 60 seconds", "Thought leadership",
         "Two entangled photons, two observers, one shared secret key. A one-minute explainer of the BBM92 protocol "
         "your students can try on NetSim Q."),
        ("From PO to first class in five weeks", "Customer story",
         "How one institute set up a simulation lab and ran its first quantum computing class within five weeks of "
         "the order."),
        ("Faculty development programme announced", "Event",
         "Our training partner Brightpath Skills Foundation and Demo are running a three-day FDP on teaching quantum "
         "computing with simulators. Registration opens next week."),
        ("Five myths about quantum labs", "Thought leadership",
         "Myth 1: you need a cryogenic lab to teach quantum computing. You don't — here's why simulators come first."),
        ("Notebook tips for instructors", "Product",
         "Our notebooks run on JupyterHub-style servers so instructors can share exercises instantly."),
        ("Women in quantum — student voices", "Hiring & careers",
         "We asked five students what drew them to quantum technologies. Their answers might surprise you."),
        ("Three questions to ask before buying a lab", "Thought leadership",
         "Who will teach it, which courses will use it, and where will it live? Answer these three and the rest of "
         "the plan follows."),
        ("A day at an FDP", "Event",
         "Forty faculty, one simulator each, and a lot of good questions. Highlights from our latest workshop."),
        ("Quantum sensing explained", "Thought leadership",
         "How do atoms measure magnetic fields? EIT and the Zeeman effect, explained without equations."),
    )

    def _content(self, d: date) -> None:
        """One post a week: idea (Mon) → draft and send for approval (Tue) → approved (Wed) → posted (Thu) →
        numbers a week later. The last few weeks stop at earlier stages, so the board shows every column."""
        from app.services.content import (
            PostData,
            approve_post,
            create_post,
            mark_posted,
            save_metrics,
            send_back,
            send_for_review,
            update_post,
        )

        week = (d - (self.first - timedelta(days=self.first.weekday()))).days // 7
        if week >= len(self.POSTS):
            return
        title, theme, body = self.POSTS[week]
        weeks_left = (self.today - d).days // 7
        author = self.kabir if week % 3 else self.neha
        post = self.s.scalar(select(ContentPost).where(ContentPost.title == title))
        wd = d.weekday()
        if wd == 0 and post is None:
            self.at(d, 10, 40)
            create_post(self.s, PostData(title=title, platform="both" if week % 4 == 0 else "linkedin", theme=theme,
                                         campaign="Autumn 2026" if week > 5 else "Launch",
                                         planned_on=d + timedelta(days=3), poster_user_id=self.kabir.id), author)
            return
        if post is None:
            return
        if wd == 1 and post.status == "idea":
            self.at(d, 14, 0)
            update_post(self.s, post.id, PostData(title=title, platform=post.platform, theme=theme,
                                                  campaign=post.campaign, planned_on=post.planned_on, body=body,
                                                  hashtags="QuantumEducation, NQM, QuantumIndia",
                                                  poster_user_id=self.kabir.id), author)
            send_for_review(self.s, post.id, author)
        elif wd == 2 and post.status == "review":
            self.at(d, 11, 0)
            if "JupyterHub" in (post.body or ""):
                send_back(self.s, post.id, "Please say Demo Notebook Studio, not JupyterHub.", self.riya)
                return
            approve_post(self.s, post.id, self.riya, planned_on=d + timedelta(days=1), poster_user_id=self.kabir.id)
        elif wd == 3 and post.status == "approved" and post.planned_on and post.planned_on <= d < self.today:
            self.at(d, 12, 0)
            mark_posted(self.s, post.id, self.kabir, url=f"https://www.linkedin.com/posts/demo-{_slug(title)}",
                        posted_on=d)
        # Numbers arrive 7 days after posting.
        for live in self.s.scalars(select(ContentPost).where(ContentPost.status == "posted",
                                                             ContentPost.metrics_on.is_(None))):
            if live.posted_on and live.posted_on + timedelta(days=7) <= d and d < self.today - timedelta(days=1):
                self.at(d, 17, 0)
                boost = {"Event": 1.6, "Student project": 1.4, "Customer story": 1.3}.get(live.theme or "", 1.0)
                likes = int(self.rng.randint(40, 110) * boost)
                save_metrics(self.s, live.id, {"likes": likes, "comments": int(likes * self.rng.uniform(0.05, 0.15)),
                                               "reposts": int(likes * self.rng.uniform(0.03, 0.1)),
                                               "impressions": likes * self.rng.randint(28, 45)}, self.kabir)

    def _content_board(self) -> None:
        """Make sure every column of the content board has something today: a post waiting for approval, one approved
        for later this week, and ideas for next week."""
        from app.services.content import PostData, approve_post, create_post, send_for_review

        used = set(self.s.scalars(select(ContentPost.title)))
        spare = [p for p in self.POSTS if p[0] not in used]
        yesterday = self.today - timedelta(days=1)
        if spare:
            title, theme, body = spare.pop(0)
            self.at(yesterday, 16, 20)
            post = create_post(self.s, PostData(title=title, platform="linkedin", theme=theme, body=body,
                                                hashtags="QuantumEducation, QuantumIndia",
                                                planned_on=self.today + timedelta(days=5),
                                                poster_user_id=self.kabir.id), self.neha)
            send_for_review(self.s, post.id, self.neha)
        if spare:
            title, theme, body = spare.pop(0)
            self.at(yesterday - timedelta(days=1), 11, 0)
            post = create_post(self.s, PostData(title=title, platform="instagram", theme=theme, body=body,
                                                hashtags="QuantumSensing, Physics",
                                                planned_on=self.today + timedelta(days=2),
                                                poster_user_id=self.kabir.id), self.kabir)
            send_for_review(self.s, post.id, self.kabir)
            self.at(yesterday, 10, 0)
            approve_post(self.s, post.id, self.riya, planned_on=self.today + timedelta(days=2),
                         poster_user_id=self.kabir.id)
        self.at(yesterday, 18, 0)
        for title, theme in (("Meet the team behind the kits", "Behind the scenes"),
                             ("FDP registrations are open", "Event")):
            create_post(self.s, PostData(title=title, platform="linkedin", theme=theme,
                                         planned_on=self.today + timedelta(days=8)), self.kabir)

    # ---------------------------------------------------------------- events and community

    def _events(self, d: date) -> None:
        from app.services.community import EventData, add_participant, create_lead_for_member, save_event

        offset = (self.today - d).days
        span = (self.today - self.first).days
        # Offsets in days before today; squeezed into shorter runs (the tests build only a few weeks).
        create, hack_reg, hack_done = min(60, span), min(50, span - 1), min(43, span - 2)
        web_reg, web_done, fdp = min(26, span - 3), min(19, span - 4), min(15, span - 5)
        if offset == create:
            self.at(d, 10, 0)
            self.hackathon = save_event(self.s, EventData(
                name="Quantum Computing Hackathon 2026", kind="Hackathon", start_on=self.today - timedelta(days=45),
                end_on=self.today - timedelta(days=44), mode="online", status="open",
                form_url="https://forms.example.com/demo-hackathon-2026", cost_inr="35000", owner_id=self.kabir.id,
                description="24-hour online hackathon on quantum algorithms and networks."), self.kabir)
            self.webinar = save_event(self.s, EventData(
                name="Intro to Quantum Networks — webinar", kind="Webinar", start_on=self.today - timedelta(days=20),
                mode="online", status="open", form_url="https://forms.example.com/demo-networks-webinar",
                owner_id=self.riya.id, description="One-hour webinar for ECE faculty and students."), self.riya)
        if offset in (hack_reg, web_reg):
            event = self.hackathon if offset == hack_reg else self.webinar
            size = 36 if offset == hack_reg else 28
            lead_orgs = [o.name for o in self.s.scalars(select(Organisation).join(Lead, Lead.org_id ==
                                                                                  Organisation.id)
                                                        .where(Organisation.org_type.in_(("college", "university"))))]
            levels = ("B.Tech CSE, 3rd year", "B.Tech ECE, final year", "M.Sc Physics", "PhD scholar, Physics",
                      "Assistant Professor, ECE", "Software engineer", "M.Tech, Communication Systems")
            teams = ["Qubit Crew", "Entangled Minds", "Superposers", "Bell Pairs", "Phase Kickers", "Q-Tensors",
                     "Spin Doctors", "Photon Pack", "Grover's Gang", "Shor Things", "Decoherence Busters"]
            for i in range(size):
                first, last = self.person()
                self.at(d, 10 + i // 6, (i * 7) % 60)
                institution = self.rng.choice(lead_orgs) if lead_orgs and self.chance(0.45) else \
                    self.rng.choice(self.pool)["name"]
                try:
                    part = add_participant(self.s, event.id, name=f"{first} {last}",
                                           email=self.email_for(first, last, "students.example"), user=self.kabir,
                                           institution=institution, education=self.rng.choice(levels),
                                           team_name=teams[i % len(teams)] if event is self.hackathon else None,
                                           consent=self.chance(0.82), is_adult=self.chance(0.95))
                except Exception:  # noqa: BLE001 — a duplicate person in the same event is simply skipped
                    continue
                part.status = "attended" if self.chance(0.88) else "no_show"
            self.s.flush()
        if offset in (hack_done, web_done):
            event = self.hackathon if offset == hack_done else self.webinar
            self.at(d, 18, 0)
            if event is self.hackathon:
                for part in self.s.scalars(select(EventParticipant).where(EventParticipant.event_id == event.id,
                                                                          EventParticipant.team_name == "Bell Pairs")):
                    part.status = "winner"
                    part.project_title = "Entanglement routing for a smart campus"
                    part.project_link = "https://github.example.com/bell-pairs/campus-qnet"
                for part in self.s.scalars(select(EventParticipant).where(EventParticipant.event_id == event.id,
                                                                          EventParticipant.team_name == "Superposers")):
                    part.status = "finalist"
            save_event(self.s, EventData(name=event.name, kind=event.kind, start_on=event.start_on,
                                         end_on=event.end_on, mode=event.mode, status="done", form_url=event.form_url,
                                         cost_inr=event.cost_inr, owner_id=event.owner_user_id,
                                         description=event.description), self.riya, event_id=event.id)
            if event is self.hackathon:
                has_lead = select(Lead.id).where(Lead.org_id == CommunityMember.org_id).exists()
                member = self.s.scalar(select(CommunityMember).where(CommunityMember.institution.is_not(None),
                                                                     CommunityMember.education.like("Assistant%"),
                                                                     ~has_lead).order_by(CommunityMember.id))
                if member is not None:
                    lead, _ = create_lead_for_member(self.s, member.id, self.track("cold_colleges").id, self.riya)
                    self.warmth[lead.id] = 0.5
        if offset == fdp:
            self.at(d, 11, 0)
            save_event(self.s, EventData(
                name="FDP: Teaching Quantum Computing with Simulators", kind="FDP",
                start_on=self.today + timedelta(days=12), end_on=self.today + timedelta(days=14), mode="hybrid",
                venue="Kalpavriksha Institute of Technology", partner="Brightpath Skills Foundation", status="open",
                form_url="https://forms.example.com/demo-fdp-simulators", cost_inr="60000", owner_id=self.riya.id,
                description="Three-day faculty development programme with our training partner."), self.riya)

    # ---------------------------------------------------------------- privacy requests (DPDP)

    def _privacy(self, d: date) -> None:
        from app.services.privacy_requests import export_person, log_request, update_request, withdraw_consent

        offset = (self.today - d).days
        if offset == 40:
            contact = self.s.scalar(select(Contact).where(Contact.verified.is_(True), Contact.email.is_not(None))
                                    .order_by(Contact.id.desc()))
            self.at(d, 11, 0)
            request = log_request(self.s, request_type="access", requester_name=contact.name,
                                  requester_email=contact.email, channel="Email", received_on=d,
                                  notes="Asked what data we hold and where it came from.", user=self.riya)
            self.access_request = request.id
            self.access_email = contact.email
        if offset == 37:
            self.at(d, 15, 0)
            export_person(self.s, self.access_email, self.riya)
            update_request(self.s, self.access_request, status="done", user=self.riya,
                           action_taken="Sent them an Excel copy of their contact record, consent log and the emails "
                                        "we logged.")
        if offset == 6:
            member = self.s.scalar(select(CommunityMember).order_by(CommunityMember.id.desc()))
            if member is None:
                return
            self.at(d, 10, 20)
            request = log_request(self.s, request_type="withdrawal", requester_name=member.name,
                                  requester_email=member.email, channel="Email", received_on=d,
                                  notes="Replied 'unsubscribe' to the webinar follow-up.", user=self.kabir)
            withdraw_consent(self.s, member.email, self.kabir)
            update_request(self.s, request.id, status="in_progress", user=self.kabir,
                           action_taken="Stopped all contact; confirmation email to send.")
        if offset == 2:
            first, last = self.person()
            self.at(d, 16, 40)
            log_request(self.s, request_type="deletion", requester_name=f"{first} {last}",
                        requester_email=self.email_for(first, last, "mail.example"), channel="Website form",
                        received_on=d, notes="Asked us to delete their details; not found in the hub yet — check "
                                             "event lists.", user=self.riya)

    # ---------------------------------------------------------------- odd jobs: ad-hoc tasks and hand-offs

    def _odd_jobs(self, d: date) -> None:
        from app.services.tasks import create_task, hand_off

        offset = (self.today - d).days
        jobs = {
            70: ("Finalise reseller payment terms", self.riya, 2),
            55: ("Update the price list for 2026–27", self.riya, 3),
            33: ("Book the venue for the FDP", self.kabir, 5),
            20: ("Send the hackathon certificates", self.kabir, 2),
            9: ("Prepare the quarterly pipeline review", self.riya, 12),
            4: ("Collect testimonials from last year's clients", self.neha, 8),
        }
        if offset in jobs:
            title, owner, due_in = jobs[offset]
            self.at(d, 9, 40)
            create_task(self.s, title=title, day=d + timedelta(days=due_in), owner_id=owner.id, user=owner)
        # Hand-offs: a call Kabir can't take goes to Arjun (and one, yesterday, to Riya — it waits on her Today).
        if offset in (45, 23, 1):
            task = self.s.scalar(select(Activity).where(
                Activity.status == "planned", Activity.owner_user_id == self.kabir.id,
                Activity.cadence_step_id.is_not(None),
                Activity.scheduled_at >= at_ist(d, time(0, 0))).order_by(Activity.scheduled_at))
            if task is not None:
                self.at(d, 16, 30)
                target = self.riya if offset == 1 else self.arjun
                hand_off(self.s, task.id, target.id, "They prefer a call in Hindi — can you take this one?"
                         if offset != 1 else "They asked for a demo slot next week; over to you.", self.kabir)

    # ---------------------------------------------------------------- the last morning, and wrapping up

    def _this_morning(self, d: date) -> None:
        """Today: a fresh enquiry has just arrived and nothing has been worked yet."""
        from app.services.queries import NewQuery, create_query

        self.at(d, 8, 35)
        first, last = self.person()
        entry = self.take_institutions(1, kind="college")
        create_query(self.s, NewQuery(
            channel="Website", received_at=datetime.now(IST), name=f"Dr. {first} {last}",
            email=self.email_for(first, last, entry[0]["domain"] if entry else "mail.example"), phone=self.phone(),
            org_id=self.org_by_code[entry[0]["code"]].id if entry else None,
            products=["QSim Cluster"], message="We would like a quotation for a 30-seat simulation lab before our "
                                               "purchase committee meets on Friday.", owner_id=self.riya.id),
            self.riya)
        from app.services.tasks import create_task

        self.at(d, 8, 45)
        newest = self.s.scalar(select(func.max(Lead.id)).where(Lead.track_id == self.track("cold_colleges").id))
        create_task(self.s, title="Verify the contacts from Tuesday's research batch", day=d, owner_id=self.neha.id,
                    user=self.neha, lead_id=newest)
        create_task(self.s, title="Find department pages for the Telangana colleges in the pool", day=d,
                    owner_id=self.neha.id, user=self.neha, at=time(15, 0))

    def _finish(self) -> None:
        from app.services.partners import register_deal
        from app.services.signals import recompute_scores

        self.at(self.today, 8, 55)
        # A registration waiting for the admin's decision (Partners page badge).
        entry = self.take_institutions(1, kind="university")
        if entry:
            reg_day = self.today - timedelta(days=2)
            self.at(reg_day, 15, 0)
            register_deal(self.s, partner_id=self.partners["northwind"].id, user=self.riya,
                          org_id=self.org_by_code[entry[0]["code"]].id,
                          product_ids=[self.product("Entangled-Photon QKD Kit").id], expected_value="2800000",
                          contact_name="Head of Physics", notes="Partner demoed the kit catalogue last week.",
                          today=reg_day)
        self._content_board()
        self.at(self.today, 8, 58)
        recompute_scores(self.s, today=self.today)
        self.s.flush()
