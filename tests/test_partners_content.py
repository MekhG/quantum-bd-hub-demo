"""Partners & deal registration, post-sale client records, the content calendar and DPDP privacy requests."""

from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    ActivityType,
    CommunityMember,
    ConsentLog,
    Contact,
    DataRequest,
    Event,
    Installation,
    Lead,
    NewsItem,
    Opportunity,
    Organisation,
    Product,
    Stage,
    Suppression,
    Track,
    User,
)
from app.services.cadence import today_ist
from app.services.content import (
    ContentError,
    PostData,
    approve_post,
    clean_post_url,
    content_week,
    create_post,
    idea_from_event,
    idea_from_news,
    mark_posted,
    normalise_hashtags,
    save_metrics,
    send_back,
    send_for_review,
    theme_performance,
    update_post,
    warnings_for,
)
from app.services.partners import (
    PartnerData,
    PartnerError,
    approve_registration,
    partners_frame,
    refresh_registrations,
    register_deal,
    reject_registration,
    save_partner,
    upgrade_to_distributor,
)
from app.services.postsale import (
    PostSaleError,
    ensure_installation,
    installations_frame,
    renewals_due,
    save_installation,
    suggested_amc,
    sync_installations,
    warranty_end,
)
from app.services.privacy import email_hash
from app.services.privacy_requests import (
    PrivacyError,
    delete_everywhere,
    export_person,
    find_records,
    log_request,
    open_counts,
    requests_frame,
    update_request,
    withdraw_consent,
)
from app.services.report_metrics import build_report


def _user(session: Session, name: str) -> User:
    return session.scalar(select(User).where(User.name.like(f"{name}%")))


def _org(session: Session, name: str, kind: str = "university") -> Organisation:
    org = Organisation(name=name, org_type=kind, state="Telangana")
    session.add(org)
    session.flush()
    return org


def _tagged(session: Session, tag: str, status: str = "planned") -> list[Activity]:
    return list(session.scalars(select(Activity).where(Activity.notes.like(f"{tag}%"), Activity.status == status)))


def _track(session: Session, code: str = "cold_colleges") -> int:
    return session.scalar(select(Track.id).where(Track.code == code))


# ---------------------------------------------------------------- partners


def test_partner_and_deal_registration(seeded: Session) -> None:
    admin, kabir = _user(seeded, "Riya"), _user(seeded, "Kabir")
    partner = save_partner(seeded, PartnerData(model="Reseller", new_org_name="Deccan EdTech Pvt Ltd",
                                               nda_signed_on=date(2026, 9, 1), territory="Telangana"), kabir)
    other = save_partner(seeded, PartnerData(model="Reseller", new_org_name="Vidya Labs LLP"), kabir)
    with pytest.raises(PartnerError, match="already a partner"):
        save_partner(seeded, PartnerData(model="Reseller", org_id=partner.org_id), kabir)

    college = _org(seeded, "Godavari Institute of Technology", "college")
    reg, overlap = register_deal(seeded, partner_id=partner.id, org_id=college.id, expected_value="12,50,000",
                                 product_ids=[seeded.scalar(select(Product.id).limit(1))], user=kabir)
    assert reg.status == "pending" and overlap == [] and reg.expected_value_inr == Decimal("1250000")
    rival, overlap = register_deal(seeded, partner_id=other.id, org_id=college.id, user=kabir)
    assert any("Registered by Deccan EdTech" in line for line in overlap)
    assert "Deccan EdTech" in rival.conflict_note
    with pytest.raises(PartnerError, match="for itself"):
        register_deal(seeded, partner_id=partner.id, org_id=partner.org_id, user=kabir)
    with pytest.raises(PermissionError):
        approve_registration(seeded, reg.id, track_id=_track(seeded), user=kabir)

    lead, message = approve_registration(seeded, reg.id, track_id=_track(seeded), user=admin)
    assert lead.partner_id == partner.id and lead.channel == "Reseller" and "Lead created" in message
    assert reg.expires_on == today_ist() + timedelta(days=60) and lead.product_fit
    with pytest.raises(PartnerError, match="holds an approved registration"):
        approve_registration(seeded, rival.id, track_id=_track(seeded), user=admin)
    with pytest.raises(PartnerError, match="Say why"):
        reject_registration(seeded, rival.id, user=admin, note=" ")
    reject_registration(seeded, rival.id, user=admin, note="Deccan registered first")
    assert rival.status == "rejected"

    # Expiry after the lock, and won follows the lead.
    assert refresh_registrations(seeded, today=today_ist() + timedelta(days=91)) == 1 and reg.status == "expired"
    reg.status = "approved"
    lead.stage = seeded.scalar(select(Stage).where(Stage.outcome == "won"))
    seeded.flush()
    refresh_registrations(seeded)
    assert reg.status == "won"


def test_distributor_upgrade_after_enough_wins(seeded: Session) -> None:
    admin = _user(seeded, "Riya")
    partner = save_partner(seeded, PartnerData(model="Reseller", new_org_name="Kaveri Systems"), admin)
    won = seeded.scalar(select(Stage).where(Stage.outcome == "won"))
    for i in range(5):
        org = _org(seeded, f"College {i}", "college")
        lead = Lead(org_id=org.id, track_id=_track(seeded), stage=won, channel="Reseller", partner_id=partner.id)
        seeded.add(lead)
        seeded.flush()
        if i < 4:
            seeded.add(Opportunity(lead_id=lead.id, title="Lab", status="won", value_inr=Decimal("500000")))
            seeded.flush()
            with pytest.raises(PartnerError, match="needed"):
                upgrade_to_distributor(seeded, partner.id, admin)
    seeded.add(Opportunity(lead_id=lead.id, title="Lab", status="won", value_inr=Decimal("500000")))
    seeded.flush()
    row = partners_frame(seeded).iloc[0]
    assert row["Upgrade"] == "5 of 5" and bool(row["Ready to upgrade"])
    upgrade_to_distributor(seeded, partner.id, admin)
    assert partner.model == "Distribution partner"


# ---------------------------------------------------------------- post-sale


def _won_deal(session: Session, name: str = "Nizam University", value: str = "2000000") -> Opportunity:
    org = _org(session, name)
    lead = Lead(org_id=org.id, track_id=_track(session), stage=session.scalar(select(Stage).where(
        Stage.outcome == "won")), channel="Direct", owner_user_id=_user(session, "Kabir").id)
    session.add(lead)
    session.flush()
    deal = Opportunity(lead_id=lead.id, title="Starter lab", status="won", value_inr=Decimal(value),
                       po_date=today_ist() - timedelta(days=30))
    session.add(deal)
    session.flush()
    return deal


def test_installation_milestones_and_reminders(seeded: Session) -> None:
    kabir = _user(seeded, "Kabir")
    deal = _won_deal(seeded)
    assert sync_installations(seeded) == 1 and sync_installations(seeded) == 0
    inst = ensure_installation(seeded, deal.id)
    assert inst.warranty_months == 12
    assert warranty_end(date(2026, 1, 15), 12) == date(2027, 1, 14)
    assert [r["Suggested AMC (₹)"] for r in suggested_amc(seeded, deal.id)] == [100000, 140000, 180000, 220000]

    today = today_ist()
    with pytest.raises(PostSaleError, match="before delivery"):
        save_installation(seeded, inst.id, {"delivered_on": today, "installed_on": today - timedelta(days=1)}, kabir)
    assert inst.delivered_on is None      # a refused save changes nothing
    accepted = today - timedelta(days=320)
    planned = save_installation(seeded, inst.id, {
        "delivered_on": accepted - timedelta(days=5), "installed_on": accepted - timedelta(days=1),
        "accepted_on": accepted, "training_on": today + timedelta(days=3), "next_phase": "Phase 2 kits",
        "upsell_on": today + timedelta(days=60)}, kabir)
    assert inst.warranty_end == warranty_end(accepted, 12)
    assert any(p.startswith("Product training") for p in planned)
    assert any(p.startswith("Warranty ending") for p in planned)      # ends in ~45 days → reminder today-ish
    assert any(p.startswith("Next phase: Phase 2 kits") for p in planned)
    training = _tagged(seeded, f"[client:{inst.id}:training]")
    assert len(training) == 1 and training[0].owner_user_id == kabir.id
    # Re-saving doesn't duplicate; ticking training done cancels its reminder; an active AMC replaces the warranty one.
    save_installation(seeded, inst.id, {"training_done": True, "amc_status": "active", "amc_start": today,
                                        "amc_end": today + timedelta(days=365), "amc_value_inr": "1,20,000"}, kabir)
    assert not _tagged(seeded, f"[client:{inst.id}:training]")
    assert not _tagged(seeded, f"[client:{inst.id}:warranty]")
    assert len(_tagged(seeded, f"[client:{inst.id}:amc]")) == 1 and inst.amc_value_inr == Decimal("120000")
    assert len(_tagged(seeded, f"[client:{inst.id}:upsell]")) == 1

    frame = installations_frame(seeded)
    row = frame.iloc[0]
    assert row["Client"] == "Nizam University" and row["Training"] == "Done" and row["Next"] == "AMC renewal"
    assert renewals_due(seeded) == 0 and renewals_due(seeded, days=400) == 1


def test_accepting_a_quote_starts_the_client_record(seeded: Session) -> None:
    from app.services.procurement import DealData, LineIn, QuoteDraft, accept_quote, create_deal, create_quote, \
        mark_quote_sent

    admin = _user(seeded, "Riya")
    org = _org(seeded, "Krishna University")
    contact = Contact(org_id=org.id, name="Dr. Rao", designation="Registrar", email="registrar@krishna-example.ac.in")
    seeded.add(contact)
    seeded.flush()
    lead = Lead(org_id=org.id, track_id=_track(seeded), stage=seeded.scalar(select(Stage).where(Stage.name == "New")),
                channel="Direct", primary_contact_id=contact.id, owner_user_id=admin.id)
    seeded.add(lead)
    seeded.flush()
    deal = create_deal(seeded, lead.id, DealData(title="Lab"), admin)
    quote = create_quote(seeded, deal.id, QuoteDraft(lines=[LineIn("Item", 1, Decimal("1000"))]), admin)
    mark_quote_sent(seeded, quote.id, admin)
    messages = accept_quote(seeded, quote.id, today_ist(), admin)
    assert any("Client record" in m for m in messages)
    assert seeded.scalar(select(Installation).where(Installation.opportunity_id == deal.id)) is not None


# ---------------------------------------------------------------- content


def test_content_flow_idea_to_metrics(seeded: Session) -> None:
    admin, kabir = _user(seeded, "Riya"), _user(seeded, "Kabir")
    post = create_post(seeded, PostData(title="Hackathon recap", theme="Event"), kabir)
    assert post.status == "idea" and post.poster_user_id == kabir.id
    with pytest.raises(ContentError, match="Write the post text"):
        send_for_review(seeded, post.id, kabir)
    update_post(seeded, post.id, PostData(title="Hackathon recap", theme="Event", hashtags="quantum, NQM #quantum",
                                          body="120 students built quantum projects using our PPKTP source."),
                kabir)
    assert post.status == "draft" and post.hashtags == "#quantum #NQM"
    notes = send_for_review(seeded, post.id, kabir)
    assert post.status == "review" and any("PPKTP" in n and "crystal" in n for n in notes)
    with pytest.raises(PermissionError):
        approve_post(seeded, post.id, kabir)
    send_back(seeded, post.id, "Use generic wording for the source", admin)
    assert post.status == "draft" and post.review_comment

    update_post(seeded, post.id, PostData(title="Hackathon recap", theme="Event",
                                          body="120 students built quantum projects with our entangled-photon kit."),
                kabir)
    send_for_review(seeded, post.id, kabir)
    day = today_ist() + timedelta(days=2)
    message = approve_post(seeded, post.id, admin, planned_on=day)
    assert post.status == "approved" and "Kabir" in message
    publish = _tagged(seeded, f"[post:{post.id}:publish]")
    assert len(publish) == 1 and publish[0].owner_user_id == kabir.id
    # A member editing the approved text sends it back to review and cancels the posting task.
    note = update_post(seeded, post.id, PostData(title="Hackathon recap", theme="Event", planned_on=day,
                                                 body="Changed text after approval."), kabir)
    assert post.status == "review" and "back for approval" in note and not _tagged(seeded, f"[post:{post.id}:publish]")
    approve_post(seeded, post.id, admin, planned_on=day)

    with pytest.raises(ContentError, match="LinkedIn or Instagram"):
        mark_posted(seeded, post.id, kabir, url="https://example.com/x")
    mark_posted(seeded, post.id, kabir, url="linkedin.com/posts/demo_123", posted_on=today_ist())
    assert post.status == "posted" and post.post_url.startswith("https://linkedin.com")
    assert _tagged(seeded, f"[post:{post.id}:publish]", "done")
    metrics = _tagged(seeded, f"[post:{post.id}:metrics]")
    assert len(metrics) == 1 and metrics[0].scheduled_at.date() >= today_ist() + timedelta(days=6)
    with pytest.raises(ContentError, match="whole numbers"):
        save_metrics(seeded, post.id, {"likes": "lots"}, kabir)
    save_metrics(seeded, post.id, {"likes": "48", "comments": 6, "reposts": 3, "impressions": "1,900"}, kabir)
    assert post.impressions == 1900 and not _tagged(seeded, f"[post:{post.id}:metrics]")

    other = create_post(seeded, PostData(title="NQM explainer", theme="NQM & policy", body="x"), kabir)
    send_for_review(seeded, other.id, kabir)
    approve_post(seeded, other.id, admin)
    mark_posted(seeded, other.id, kabir, url="https://www.instagram.com/p/abc/")
    save_metrics(seeded, other.id, {"likes": 10, "comments": 1}, kabir)
    themes = theme_performance(seeded)
    assert list(themes["Theme"]) == ["Event", "NQM & policy"]
    assert themes.iloc[0]["Avg engagements per post"] == 57 and themes.iloc[0]["Engagement rate %"] == 3.0

    week = content_week(seeded, today_ist() - timedelta(days=today_ist().weekday()),
                        today_ist() - timedelta(days=today_ist().weekday()) + timedelta(days=6))
    assert week["posted"] == 2 and week["best_post"][0] == "Hackathon recap"
    report = build_report(seeded, today_ist() - timedelta(days=today_ist().weekday()))
    assert report["content"]["posted"] == 2 and report["content"]["themes"][0]["Theme"] == "Event"
    import json
    json.dumps(report["content"])   # finalised reports are stored as JSON


def test_content_helpers(seeded: Session) -> None:
    kabir = _user(seeded, "Kabir")
    assert normalise_hashtags("#AI, ai;  Quantum-India") == "#AI #QuantumIndia"
    assert clean_post_url("") is None
    assert clean_post_url("https://lnkd.in/abc") == "https://lnkd.in/abc"
    post = PostData(title="t", platform="instagram", body="Runs on JupyterHub " + "x" * 2300)
    notes = warnings_for(seeded, post)
    assert any("Demo Notebook Studio" in n for n in notes) and any("2,200" in n for n in notes)

    item = NewsItem(url="https://example.com/nqm", title="DST announces NQM teaching labs for 50 colleges")
    event = Event(name="Quantum Hackathon 2026", kind="Hackathon", start_on=today_ist() + timedelta(days=30))
    seeded.add_all([item, event])
    seeded.flush()
    idea = idea_from_news(seeded, item.id, kabir)
    assert idea.theme == "NQM & policy" and idea.asset_links == item.url and idea.status == "idea"
    assert idea_from_news(seeded, item.id, kabir).id == idea.id    # once per article
    announce = idea_from_event(seeded, event.id, kabir)
    assert announce.title.startswith("Announce") and announce.planned_on == event.start_on - timedelta(days=14)


# ---------------------------------------------------------------- privacy


def _person(session: Session, email: str = "Asha.Menon@omega-example.ac.in") -> tuple[Contact, CommunityMember]:
    org = _org(session, "Omega University")
    contact = Contact(org_id=org.id, name="Dr. Asha Menon", email=email)
    member = CommunityMember(name="Asha Menon", email=email.lower(), consent=True, is_adult=True)
    session.add_all([contact, member])
    session.flush()
    session.add(ConsentLog(contact_id=contact.id, source="University website", basis="publicly_available"))
    call = Activity(type_id=session.scalar(select(ActivityType.id).limit(1)), title="Intro call", org_id=org.id,
                    status="done")
    session.add(call)
    session.flush()
    session.add(ActivityParticipant(activity_id=call.id, contact_id=contact.id))
    session.flush()
    return contact, member


def test_privacy_request_log_find_and_export(seeded: Session) -> None:
    kabir = _user(seeded, "Kabir")
    contact, member = _person(seeded)
    with pytest.raises(PrivacyError, match="doesn't look right"):
        log_request(seeded, request_type="access", requester_name="A", requester_email="not-an-email", channel="Email",
                    received_on=None, notes=None, user=kabir)
    request = log_request(seeded, request_type="access", requester_name="Dr. Asha Menon",
                          requester_email=" ASHA.menon@omega-example.ac.in ", channel="Email",
                          received_on=today_ist() - timedelta(days=31), notes="Wants a copy", user=kabir)
    assert request.due_on == today_ist() - timedelta(days=1) and request.contact_id == contact.id
    assert request.community_member_id == member.id
    assert open_counts(seeded) == (1, 1)
    frame = requests_frame(seeded)
    assert bool(frame.iloc[0]["Overdue"]) and frame.iloc[0]["Status"] == "Open"

    found = find_records(seeded, "asha.menon@omega-example.ac.in")
    assert len(found["contacts"]) == 1 and len(found["members"]) == 1 and found["activities"] == 1
    data = export_person(seeded, "asha.menon@omega-example.ac.in", kabir)
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
    assert {"Contact details", "Consent", "Interactions", "Community", "Events"} <= set(sheets)
    assert sheets["Interactions"].iloc[0]["What"] == "Intro call"

    with pytest.raises(PrivacyError, match="what was done"):
        update_request(seeded, request.id, status="done", action_taken="", user=kabir)
    update_request(seeded, request.id, status="done", action_taken="Sent the export by email", user=kabir)
    assert request.closed_on == today_ist() and open_counts(seeded) == (0, 0)


def test_withdraw_and_delete_everywhere(seeded: Session) -> None:
    admin, kabir = _user(seeded, "Riya"), _user(seeded, "Kabir")
    contact, member = _person(seeded)
    email = "asha.menon@omega-example.ac.in"
    message = withdraw_consent(seeded, email, kabir)
    assert "1 contact record" in message and contact.do_not_contact and member.do_not_contact and not member.consent
    assert seeded.scalar(select(ConsentLog.withdrawn_on)) == today_ist()
    suppression = seeded.scalar(select(Suppression).where(Suppression.email_sha256 == email_hash(email)))
    assert suppression.reason == "DPDP: consent withdrawn"

    request = log_request(seeded, request_type="deletion", requester_name=None, requester_email=email,
                          channel="Email", received_on=None, notes=None, user=kabir)
    with pytest.raises(PermissionError):
        delete_everywhere(seeded, email, kabir)
    delete_everywhere(seeded, email, admin)
    seeded.commit()
    assert seeded.scalar(select(Contact).where(Contact.email.ilike(email))) is None
    assert seeded.scalar(select(CommunityMember).where(CommunityMember.email == email)) is None
    assert seeded.scalar(select(ActivityParticipant).where(ActivityParticipant.contact_id.is_not(None))) is None
    assert seeded.scalar(select(Activity.title)) == "Intro call"           # the activity stays, without her
    assert suppression.reason == "DPDP deletion request"                   # upgraded from withdrawal
    kept = seeded.get(DataRequest, request.id)
    assert kept.requester_email == email and kept.contact_id is None


def test_import_history_reads_both_summary_shapes() -> None:
    # A community import (Week 9) stores "label: value" strings; that broke the Dashboard's recent imports.
    from app.services.lead_queries import _summary_text

    assert _summary_text(["new members: 9", "updated members: 0"]) == "new members: 9, updated members: 0"
    assert _summary_text([["Leads", 3], ["Contacts", 5]]) == "Leads: 3, Contacts: 5"
    assert _summary_text({"Leads": 1}) == "Leads: 1" and _summary_text(None) == ""
