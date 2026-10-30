"""Partners and deal registration: resellers, distribution, referral, training and technology partners; the deals they
register for organisations; the overlap check; and admin approval that locks the organisation to the partner."""

from __future__ import annotations


import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.services.cadence import today_ist
from app.models import Organisation, Product, Track, User
from app.models.partners import PARTNER_STATUSES
from app.services.lead_queries import frame_to_xlsx
from app.services.library import shareable_documents
from app.services.money import inr
from app.services.partners import (
    PartnerData,
    PartnerError,
    approve_registration,
    check_overlap,
    partner_detail,
    partners_frame,
    register_deal,
    registrations_frame,
    reject_registration,
    save_partner,
    setting_int,
    upgrade_to_distributor,
)
from app.services.procurement import options
from app.ui.common import require_user
from app.ui.components import card, chip, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
FLASH = "edit_flash"
NEW_ORG = "— Not in the list: type the name —"


def _done(message: str, kind: str = "success") -> None:
    st.session_state[FLASH] = (kind, message, [])
    st.cache_data.clear()
    st.rerun()


def _me(session) -> User:
    return session.get(User, user.id)


@st.cache_data(ttl=3600, show_spinner=False)
def _choices() -> dict:
    with session_scope() as session:
        orgs = list(session.execute(select(Organisation.id, Organisation.name, Organisation.city)
                                    .order_by(Organisation.name)).all())
        seen: dict[str, int] = {}
        for _, name, _ in orgs:
            seen[name] = seen.get(name, 0) + 1
        labels = {(name if seen[name] == 1 else f"{name} · {city or '#' + str(oid)}"): oid for oid, name, city in orgs}
        return {
            "orgs": labels,
            "models": options(session, "partner_model") or ["Reseller", "Distribution partner", "Other"],
            "tracks": [(t.id, t.name) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                               .order_by(Track.sort_order))],
            "products": [(p.id, p.name) for p in session.scalars(select(Product).where(Product.is_active.is_(True))
                                                                 .order_by(Product.name))],
            "team": [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True))
                                                             .order_by(User.id))],
            "documents": shareable_documents(session),
            "lock_days": setting_int(session, "deal_registration_days", 60),
        }


@st.cache_data(ttl=600, show_spinner=False)
def _partners() -> pd.DataFrame:
    with session_scope() as session:
        return partners_frame(session)


@st.cache_data(ttl=600, show_spinner=False)
def _registrations() -> pd.DataFrame:
    with session_scope() as session:
        return registrations_frame(session)


def _org_picker(label: str, key: str, exclude: int | None = None) -> tuple[int | None, str | None]:
    choices = _choices()["orgs"]
    names = [NEW_ORG] + [n for n, oid in choices.items() if oid != exclude]
    pick = st.selectbox(label, names, index=None, placeholder="Start typing a name…", key=key)
    if pick == NEW_ORG:
        typed = st.text_input("Name", key=f"{key}_new", placeholder="Full name, as on their letterhead")
        return None, typed
    return (choices.get(pick) if pick else None), None


# ---------------------------------------------------------------- dialogs


@st.dialog("Partner", width="large")
def partner_dialog(partner_id: int | None = None) -> None:
    c = _choices()
    current = None
    if partner_id:
        with session_scope() as session:
            current = partner_detail(session, partner_id)
    p = current or {}
    org_id, new_name = None, None
    if current:
        st.markdown(f"**{esc(p['name'])}**")
    else:
        org_id, new_name = _org_picker("Partner company", "partner_org")
    a, b = st.columns(2)
    models = c["models"]
    model = a.selectbox("Partner model", models, index=models.index(p["model"]) if p.get("model") in models else 0)
    status = b.selectbox("Status", PARTNER_STATUSES, format_func=str.title,
                         index=PARTNER_STATUSES.index(p.get("status", "active")))
    a, b, d = st.columns([1, 1, 1])
    nda = a.date_input("NDA signed on", value=p.get("nda_signed_on"), format="DD/MM/YYYY")
    non_compete = b.checkbox("Non-compete agreed", value=p.get("non_compete", False))
    team_ids = [uid for uid, _ in c["team"]]
    owner_default = p.get("owner_id") or user.id
    owner = d.selectbox("Our owner", team_ids, format_func=dict(c["team"]).get,
                        index=team_ids.index(owner_default) if owner_default in team_ids else 0)
    docs = [None] + [did for did, _ in c["documents"]]
    doc_label = {None: "—", **dict(c["documents"])}
    a, b = st.columns(2)
    nda_doc = a.selectbox("Signed NDA (Library)", docs, format_func=doc_label.get,
                          index=docs.index(p.get("nda_doc_id")) if p.get("nda_doc_id") in docs else 0,
                          help="Upload the signed copy to the Library first.")
    terms_doc = b.selectbox("Partner terms / agreement (Library)", docs, format_func=doc_label.get,
                            index=docs.index(p.get("terms_doc_id")) if p.get("terms_doc_id") in docs else 0)
    a, b = st.columns(2)
    price = a.text_input("Partner price list version", value=p.get("price_list_version", ""),
                         placeholder="e.g. 2026-27 v2")
    territory = b.text_input("Territory", value=p.get("territory", ""), placeholder="e.g. Telangana, Andhra Pradesh")
    contact_id = None
    if current and p["contacts"]:
        ids = [None] + [cid for cid, _ in p["contacts"]]
        label = {None: "—", **dict(p["contacts"])}
        contact_id = st.selectbox("Main contact", ids, format_func=label.get,
                                  index=ids.index(p.get("contact_id")) if p.get("contact_id") in ids else 0)
    elif current:
        st.caption("Add the partner's people on the Contacts page (organisation = the partner) to pick a main contact.")
    terms = st.text_area("Payment terms", value=p.get("payment_terms", ""), height=68,
                         placeholder="e.g. 50% with PO, 50% on delivery; partner margin as per price list")
    notes = st.text_area("Notes", value=p.get("notes", ""), height=68)
    if st.button("Save partner", type="primary", icon=":material/save:"):
        try:
            with session_scope() as session:
                save_partner(session, PartnerData(
                    model=model, org_id=org_id, new_org_name=new_name, status=status, nda_signed_on=nda,
                    non_compete=non_compete, nda_doc_id=nda_doc, terms_doc_id=terms_doc, price_list_version=price,
                    payment_terms=terms, contact_id=contact_id, owner_id=owner, territory=territory, notes=notes),
                    _me(session), partner_id=partner_id)
        except (PartnerError, PermissionError) as error:
            st.error(str(error))
            return
        _done("Partner saved.")


@st.dialog("Register a deal", width="large")
def register_dialog(partner_id: int | None = None) -> None:
    c = _choices()
    partners = _partners()
    usable = partners[partners["Status"].isin(["Active", "Prospect"])]
    if usable.empty:
        st.info("Add a partner first (New partner).")
        return
    ids = list(usable["id"].astype(int))
    names = dict(zip(ids, usable["Partner"]))
    partner = st.selectbox("Partner", ids, format_func=names.get,
                           index=ids.index(partner_id) if partner_id in ids else 0)
    st.caption("Registered on the partner's behalf, from their email or call. An admin approves it; once approved "
               f"the organisation is theirs for {c['lock_days']} days.")
    with session_scope() as session:
        partner_org = partner_detail(session, partner)["org_id"]
    org_id, new_name = _org_picker("Organisation", "reg_org", exclude=partner_org)
    if org_id:
        with session_scope() as session:
            overlap = check_overlap(session, org_id, partner)
        if overlap:
            st.warning("**Already known:**\n\n" + "\n".join(f"- {line}" for line in overlap), icon=":material/info:")
        else:
            st.success("No overlap — not our lead and no other partner has registered it.",
                       icon=":material/check_circle:")
    product_ids = st.multiselect("Products", [pid for pid, _ in c["products"]], format_func=dict(c["products"]).get,
                                 placeholder="What they're proposing")
    a, b = st.columns(2)
    value = a.text_input("Expected value (₹, excl. GST)", placeholder="e.g. 12,50,000")
    contact = b.text_input("Their contact at the organisation", placeholder="Name and role, e.g. Dr. Rao, HoD Physics")
    notes = st.text_area("Notes", height=68, placeholder="Stage of discussion, timeline, budget source…")
    if st.button("Register", type="primary", icon=":material/how_to_reg:", disabled=not user.can("member")):
        try:
            with session_scope() as session:
                reg, overlap = register_deal(session, partner_id=partner, org_id=org_id, new_org_name=new_name,
                                             product_ids=product_ids, expected_value=value, contact_name=contact,
                                             notes=notes, user=_me(session))
        except (PartnerError, PermissionError) as error:
            st.error(str(error))
            return
        _done("Registered — waiting for admin approval." + (" Overlap noted for the approver." if overlap else ""))


# ---------------------------------------------------------------- pending approvals


def pending_cards(pending: pd.DataFrame) -> None:
    c = _choices()
    track_ids = [tid for tid, _ in c["tracks"]]
    default_track = next((tid for tid, name in c["tracks"] if "fresh" in name.lower()), track_ids[0] if track_ids
                         else None)
    for row in pending.to_dict("records"):
        rid = int(row["id"])
        with card(f"reg-{rid}", tone="plain"):
            value = inr(row["Expected value (₹)"], decimals=False) if pd.notna(row["Expected value (₹)"]) else ""
            html_block(f'<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">'
                       f'<div><b>{esc(row["Organisation"])}</b> <span class="qa-muted">via</span> '
                       f'<b>{esc(row["Partner"])}</b></div>'
                       f'{chips([("Waiting", "orange"), row["Registered"].strftime("%d %b %Y"), value])}</div>'
                       + (f'<div class="qa-small">{esc(row["Products"])}</div>' if row["Products"] else "")
                       + (f'<div class="qa-small qa-muted">Their contact: {esc(row["Their contact"])}</div>'
                          if row["Their contact"] else ""))
            if row["Overlap found"]:
                st.warning(row["Overlap found"], icon=":material/warning:")
            if not user.can("admin"):
                st.caption("An admin decides.")
                continue
            a, b, d = st.columns([1.4, 2, 1.2], vertical_alignment="bottom")
            track = a.selectbox("Track for the lead", track_ids, format_func=dict(c["tracks"]).get,
                                index=track_ids.index(default_track) if default_track in track_ids else 0,
                                key=f"reg_track_{rid}")
            note = b.text_input("Note (needed to reject)", key=f"reg_note_{rid}")
            with d, st.container(horizontal=True, gap="small"):
                approve = st.button("Approve", type="primary", key=f"reg_ok_{rid}")
                reject = st.button("Reject", key=f"reg_no_{rid}")
            if approve or reject:
                try:
                    with session_scope() as session:
                        if approve:
                            _, message = approve_registration(session, rid, track_id=track, note=note,
                                                              user=_me(session))
                        else:
                            reject_registration(session, rid, note=note, user=_me(session))
                            message = "Rejected."
                except (PartnerError, PermissionError) as error:
                    st.error(str(error))
                else:
                    _done(message)


# ---------------------------------------------------------------- one partner


def partner_panel(row: dict) -> None:
    pid = int(row["id"])
    with session_scope() as session:
        p = partner_detail(session, pid)
    if p is None:
        return
    with card(f"partner-{pid}"):
        info, actions = st.columns([3, 2], vertical_alignment="top")
        tone = {"Active": "green", "Prospect": "blue", "Paused": "orange", "Ended": "red"}.get(row["Status"],
                                                                                             "neutral")
        with info:
            html_block(f'<h3 style="margin:0;padding:0">{esc(p["name"])}</h3>'
                       f'<div style="margin-top:4px">{chips([(p["model"], "purple"), (row["Status"], tone), p["territory"]])}'
                       f'</div>')
            facts = [("NDA", p["nda_signed_on"].strftime("%d %b %Y") if p["nda_signed_on"] else "Not signed"),
                     ("Non-compete", "Yes" if p["non_compete"] else "No"),
                     ("Price list", p["price_list_version"] or "—"), ("Main contact", p["contact"] or "—")]
            html_block('<div class="qa-small" style="margin-top:6px">' + " · ".join(
                f'<span class="qa-muted">{esc(k)}:</span> {esc(v)}' for k, v in facts) + "</div>")
            if p["payment_terms"]:
                st.caption(f"Payment terms: {p['payment_terms']}")
            if p["model"] == "Reseller":
                st.progress(min(p["won"] / p["threshold"], 1.0) if p["threshold"] else 0.0,
                            text=f"Distribution partner upgrade: {min(p['won'], p['threshold'])} of {p['threshold']} "
                                 "won sales")
        with actions, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            if st.button("Edit", icon=":material/edit:", key=f"p_edit_{pid}", disabled=not user.can("member")):
                partner_dialog(pid)
            if st.button("Register a deal", icon=":material/how_to_reg:", key=f"p_reg_{pid}",
                         disabled=not user.can("member")):
                register_dialog(pid)
            if p["model"] == "Reseller" and p["won"] >= p["threshold"] and user.can("admin"):
                if st.button("Make distribution partner", type="primary", key=f"p_up_{pid}",
                             icon=":material/upgrade:"):
                    with session_scope() as session:
                        _done(upgrade_to_distributor(session, pid, _me(session)))
    regs = _registrations()
    mine = regs[regs["Partner"] == p["name"]]
    if mine.empty:
        st.caption("No deals registered yet.")
    else:
        st.dataframe(_rupees(mine.drop(columns=["id", "Partner", "lead_id"])), hide_index=True, width="stretch",
                     column_config=_reg_columns())


def _reg_columns() -> dict:
    return {"Registered": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Locked until": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Expected value (₹)": st.column_config.TextColumn(width="small")}


def _rupees(frame: pd.DataFrame) -> pd.DataFrame:
    # Indian grouping (₹12,50,000) as text for display; the Excel download keeps the plain numbers.
    out = frame.copy()
    out["Expected value (₹)"] = out["Expected value (₹)"].map(lambda v: inr(v, decimals=False) if pd.notna(v) else "")
    return out


# ---------------------------------------------------------------- page

head, tools = st.columns([3.2, 2], vertical_alignment="bottom")
with head:
    page_header("Partners", "Resellers, distribution, referral, training and technology partners — and the deals they "
                "register, so two partners (or a partner and us) never chase the same organisation.",
                eyebrow="Pipeline")
with tools, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
    if st.button("Register a deal", icon=":material/how_to_reg:", disabled=not user.can("member")):
        register_dialog()
    if st.button("New partner", icon=":material/add:", type="primary", disabled=not user.can("member")):
        partner_dialog()

partners = _partners()
regs = _registrations()
if partners.empty:
    empty_state("No partners yet. Add one with New partner — a reseller, distribution, referral, "
                "training or technology partner.")
    st.stop()

pending = regs[regs["Status"] == "Pending"]
today = today_ist()
locked = regs[(regs["Status"] == "Approved") & (regs["Locked until"].fillna(today) >= today)] if not regs.empty \
    else regs
m = st.columns(4)
m[0].metric("Active partners", int((partners["Status"] == "Active").sum()), border=True)
m[1].metric("Waiting for approval", len(pending), border=True)
m[2].metric("Locked registrations", len(locked), border=True, help="Approved and still inside the lock period.")
m[3].metric("Won through partners", int(partners["Won sales"].sum()), border=True)

tab_partners, tab_regs = st.tabs(["Partners", f"Deal registrations · {len(pending)} waiting" if len(pending)
                                  else "Deal registrations"])
with tab_partners:
    ready = partners[partners["Ready to upgrade"]]
    if not ready.empty:
        st.info(f"Ready to become distribution partners: {', '.join(ready['Partner'])}.", icon=":material/upgrade:")
    shown = partners.drop(columns=["id"])
    picked = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                          key="partners_table", height=min(400, 35 * (len(shown) + 1) + 3),
                          column_config={"NDA signed": st.column_config.DateColumn(format="DD MMM YYYY"),
                                         "Non-compete": st.column_config.CheckboxColumn(width="small"),
                                         "Ready to upgrade": st.column_config.CheckboxColumn(width="small")})
    rows = picked.selection.rows if picked and picked.selection else []
    st.download_button("Partners (Excel)", data=frame_to_xlsx(shown, "Partners"), icon=":material/download:",
                       file_name=f"partners-{today.isoformat()}.xlsx")
    if rows:
        partner_panel(partners.iloc[rows[0]].to_dict())
    else:
        st.caption("Tick a partner at the left edge to see its details and registrations.")
with tab_regs:
    if pending.empty:
        st.caption("Nothing waiting for a decision.")
    else:
        pending_cards(pending)
    if not regs.empty:
        st.markdown("**All registrations**")
        statuses = st.multiselect("Status", sorted(regs["Status"].unique()), placeholder="All statuses",
                                  key="reg_status")
        view = regs[regs["Status"].isin(statuses)] if statuses else regs
        shown = view.drop(columns=["id", "lead_id"])
        st.dataframe(_rupees(shown), hide_index=True, width="stretch", height=min(420, 35 * (len(shown) + 1) + 3),
                     column_config=_reg_columns())
        st.download_button("Registrations (Excel)", data=frame_to_xlsx(shown, "Deal registrations"),
                           icon=":material/download:", file_name=f"deal-registrations-{today.isoformat()}.xlsx")
        html_block(f'<div class="qa-small qa-muted">{chip("Approved", "green")} locks the organisation to the partner; '
                   'when the lead is won or lost the registration follows, and it expires after the lock period.</div>')
