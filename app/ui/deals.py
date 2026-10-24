"""Deals, procurement checklists and quotes — shown on the lead record's Deals tab and on the Quotes page."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Contact, Lead, Opportunity, Product, Quote, User
from app.services.cadence import today_ist
from app.services.library import content_flags, flags_message
from app.services.money import inr
from app.services.procurement import (
    CHECKLIST_LABELS,
    SECTION_ORDER,
    DealData,
    LineIn,
    ProcurementError,
    QuoteDraft,
    accept_quote,
    active_price_items,
    add_checklist_item,
    bundles,
    cancel_quote,
    checklist,
    compute_totals,
    create_deal,
    create_quote,
    deals_for_lead,
    draft_from_quote,
    get_settings,
    institution_code,
    lines_from_bundle,
    lines_from_price_items,
    mark_quote_sent,
    options,
    quote_docx,
    quotes_for_deal,
    revise_quote,
    set_checklist_item,
    set_quote_link,
    suggest_route,
    update_deal,
    update_quote,
)
from app.ui.common import CurrentUser
from app.ui.components import card, chip, chips, esc, html_block

FLASH = "edit_flash"
STATUS_TONE = {"draft": "orange", "sent": "blue", "accepted": "green", "expired": "red", "superseded": "neutral",
               "cancelled": "neutral", "open": "blue", "won": "green", "lost": "red"}
SECTION_LABELS = {"software": "Software kits", "hardware": "Hardware kits", "other": "Services / other"}
LINE_COLUMNS = ["Item", "Details", "Section", "Qty", "Unit price (INR)", "GST %", "price_item_id", "product_id"]


def _flash(message: str, kind: str = "success", details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=600, show_spinner=False)
def _docx(quote_id: int, stamp: str) -> tuple[bytes, str]:
    # stamp = the quote's updated_at, so an edited quote gets a fresh file.
    with session_scope() as session:
        return quote_docx(session, quote_id)


# ---------------------------------------------------------------- deals


@st.dialog("Deal", width="large")
def deal_dialog(lead_id: int, user: CurrentUser, deal_id: int | None = None) -> None:
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        products = [(p.id, p.name) for p in session.scalars(select(Product).where(Product.is_active.is_(True))
                                                            .order_by(Product.sort_order))]
        routes, funding = options(session, "buying_route"), options(session, "funding_source")
        suggested = suggest_route(lead.organisation.management)
        deal = session.get(Opportunity, deal_id) if deal_id else None
        current = None if deal is None else {
            "title": deal.title, "route": deal.buying_route, "funding": deal.funding_source,
            "close": deal.expected_close, "notes": deal.notes or "",
            "products": [i.product_id for i in deal.line_items],
            "qty": {i.product_id: i.quantity for i in deal.line_items}}
    title = st.text_input("Deal title", value=current["title"] if current else "", key=f"dl_title_{deal_id}",
                          placeholder="e.g. Starter simulation lab")
    a, b = st.columns(2)
    route_opts = ["", *routes]
    default_route = (current["route"] if current else suggested) or ""
    route = a.selectbox("Buying route", route_opts, index=route_opts.index(default_route) if default_route
                        in route_opts else 0, format_func=lambda r: r or "Not known yet", key=f"dl_route_{deal_id}",
                        help="Sets the document checklist. Suggested from the institution's management type "
                             "(Demo is not on GeM, so government institutions use the PAC route).")
    fund_opts = ["", *funding]
    fund = b.selectbox("Funding source", fund_opts, index=fund_opts.index(current["funding"])
                       if current and current["funding"] in fund_opts else 0,
                       format_func=lambda f: f or "Not known yet", key=f"dl_fund_{deal_id}")
    close = a.date_input("Expected close", value=current["close"] if current else None, format="DD/MM/YYYY",
                         key=f"dl_close_{deal_id}", help="January–March closes are flagged as the Q4 budget window.")
    chosen = b.multiselect("Products", [p for p, _ in products], format_func=dict(products).get,
                           default=current["products"] if current else [], key=f"dl_prod_{deal_id}")
    notes = st.text_area("Notes", value=current["notes"] if current else "", height=70, key=f"dl_notes_{deal_id}")
    if st.button("Save deal", type="primary", icon=":material/save:"):
        data = DealData(title=title, products=[(p, (current or {}).get("qty", {}).get(p, 1)) for p in chosen],
                        funding_source=fund or None, buying_route=route or None, expected_close=close, notes=notes)
        try:
            with session_scope() as session:
                actor = session.get(User, user.id)
                if deal_id:
                    update_deal(session, deal_id, data, actor)
                else:
                    create_deal(session, lead_id, data, actor)
        except (ProcurementError, PermissionError) as error:
            st.error(str(error))
            return
        _flash("Deal saved." + (" Checklist set for " + route + "." if route else ""))


def _checklist(deal_id: int, user: CurrentUser) -> None:
    with session_scope() as session:
        items = checklist(session, deal_id)
        frame = pd.DataFrame([{"id": i.id, "Document": i.item, "Status": CHECKLIST_LABELS[i.status],
                               "Notes": i.notes or ""} for i in items], columns=["id", "Document", "Status", "Notes"])
    st.markdown("**Procurement checklist**")
    if frame.empty:
        st.caption("Pick a buying route on the deal to get its document checklist.")
    else:
        done = int(frame["Status"].isin([CHECKLIST_LABELS["submitted"], CHECKLIST_LABELS["not_needed"]]).sum())
        html_block(chip(f"{done} of {len(frame)} done", "green" if done == len(frame) else "blue"))
        edited = st.data_editor(frame, hide_index=True, width="stretch", key=f"ck_{deal_id}",
                                disabled=["id", "Document"] if user.can("member") else True,
                                column_order=["Document", "Status", "Notes"],
                                column_config={"Status": st.column_config.SelectboxColumn(
                                    options=list(CHECKLIST_LABELS.values()), required=True, width="small"),
                                    "Document": st.column_config.TextColumn(width="large")})
        changed = edited[(edited["Status"] != frame["Status"]) | (edited["Notes"] != frame["Notes"])]
        if not changed.empty and st.button(f"Save checklist ({len(changed)})", key=f"ck_save_{deal_id}",
                                           type="primary"):
            reverse = {v: k for k, v in CHECKLIST_LABELS.items()}
            with session_scope() as session:
                actor = session.get(User, user.id)
                for row in changed.to_dict("records"):
                    set_checklist_item(session, int(row["id"]), actor, status=reverse[row["Status"]],
                                       notes=row["Notes"] or "")
            _flash("Checklist saved.")
    if user.can("member"):
        with st.popover("Add a checklist item", icon=":material/playlist_add:"):
            text = st.text_input("What's needed", key=f"ck_new_{deal_id}", placeholder="e.g. Vendor registration form")
            if st.button("Add", key=f"ck_add_{deal_id}", type="primary"):
                try:
                    with session_scope() as session:
                        add_checklist_item(session, deal_id, text, session.get(User, user.id))
                except ProcurementError as error:
                    st.error(str(error))
                else:
                    _flash("Added to the checklist.")


# ---------------------------------------------------------------- quote builder


def _lines_frame(lines: list[LineIn]) -> pd.DataFrame:
    return pd.DataFrame([{"Item": line.description, "Details": line.details or "",
                          "Section": SECTION_LABELS.get(line.section, SECTION_LABELS["other"]),
                          "Qty": int(line.quantity), "Unit price (INR)": float(line.unit_price),
                          "GST %": float(line.gst_pct), "price_item_id": line.price_item_id,
                          "product_id": line.product_id} for line in lines], columns=LINE_COLUMNS)


def _frame_lines(frame: pd.DataFrame) -> list[LineIn]:
    reverse = {v: k for k, v in SECTION_LABELS.items()}
    lines = []
    for i, rec in enumerate(frame.to_dict("records"), start=1):
        name = str(rec.get("Item") or "").strip()
        if not name:
            continue
        try:
            price = Decimal(str(rec.get("Unit price (INR)") or 0))
            gst = Decimal(str(rec.get("GST %") if rec.get("GST %") is not None else 18))
        except InvalidOperation as error:
            raise ProcurementError(f"Item {i}: price and GST must be numbers.") from error

        def _id(value):
            return None if value is None or pd.isna(value) else int(value)

        lines.append(LineIn(description=name, details=str(rec.get("Details") or "").strip() or None,
                            section=reverse.get(rec.get("Section"), "other"), quantity=int(rec.get("Qty") or 1),
                            unit_price=price, gst_pct=gst, price_item_id=_id(rec.get("price_item_id")),
                            product_id=_id(rec.get("product_id"))))
    return lines


@st.dialog("Quotation", width="large")
def quote_dialog(deal_id: int, user: CurrentUser, quote_id: int | None = None, mode: str = "new") -> None:
    """mode: new / edit (a draft) / revise (a new version of a sent quote)."""
    key = f"qb_{mode}_{deal_id}_{quote_id or 0}"
    with session_scope() as session:
        deal = session.get(Opportunity, deal_id)
        lead = session.get(Lead, deal.lead_id)
        org_name = lead.organisation.name
        contacts = [(c.id, " · ".join(x for x in (c.name, c.designation) if x)) for c in session.scalars(
            select(Contact).where(Contact.org_id == lead.org_id).order_by(Contact.name))]
        main_contact = lead.primary_contact_id
        settings = get_settings(session)
        price_items = [(p.id, p.name, p.institute_price_inr, p.partner_price_inr) for p in active_price_items(session)]
        bundle_list = [(b.id, f"{b.phase + ' — ' if b.phase else ''}{b.name}") for b in bundles(session)]
        existing = session.get(Quote, quote_id) if quote_id else None
        draft = draft_from_quote(existing) if existing else None
        number = f"{existing.number} v{existing.version}" if existing else None
    if key not in st.session_state:
        st.session_state[key] = {"lines": _lines_frame(draft.lines if draft else []), "v": 0}
    state = st.session_state[key]
    st.markdown(f"**{org_name}** · {'Revision of ' + number if mode == 'revise' else (number or 'New quotation')}")

    a, b, c, d = st.columns([1.3, 1, 1, 1])
    basis = a.radio("Prices", ["institute", "partner"], format_func={"institute": "Institute", "partner":
                                                                     "Partner (reseller)"}.get, horizontal=True,
                    index=["institute", "partner"].index(draft.price_basis) if draft else 0, key=f"{key}_basis")
    discount = b.number_input("Discount %", min_value=0.0, max_value=100.0, step=0.5,
                              value=float(draft.discount_pct) if draft else 0.0, key=f"{key}_disc")
    valid_days = c.number_input("Valid for (days)", min_value=1, max_value=365, step=1,
                                value=int(draft.valid_days) if draft and draft.valid_days else
                                int(settings.get("quote_validity_days") or 30), key=f"{key}_days")
    ref_code = d.text_input("Reference code", value=institution_code(org_name), key=f"{key}_code",
                            disabled=mode != "new", help="The institution part of the reference, e.g. ALU in "
                                                         "DEMO/SVU/2026-27/001.")

    with st.container(border=True):
        st.markdown("**Add items**")
        x, y = st.columns([3, 2])
        with x, st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            picks = st.multiselect("From the price list", [p[0] for p in price_items],
                                   format_func=lambda pid: next(
                                       f"{n} — {inr(i if basis == 'institute' else pp, decimals=False) if (i if basis == 'institute' else pp) is not None else 'no ' + basis + ' price'}"
                                       for p_id, n, i, pp in price_items if p_id == pid),
                                   placeholder="Pick items" if price_items else "Price list is empty",
                                   key=f"{key}_picks_{state['v']}", disabled=not price_items)
            add_items = st.button("Add", key=f"{key}_add_items", disabled=not picks)
        with y, st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            bundle = st.selectbox("Or a bundle", [b_[0] for b_ in bundle_list], format_func=dict(bundle_list).get,
                                  index=None, placeholder="No bundles yet" if not bundle_list else "Pick a bundle",
                                  key=f"{key}_bundle_{state['v']}", disabled=not bundle_list)
            add_bundle = st.button("Add", key=f"{key}_add_bundle", disabled=bundle is None)
        if not price_items:
            st.caption("No price list yet — type items straight into the table below, or fill Configuration → "
                       "Price list.")

    st.caption("Edit any cell. Add a row at the bottom for a custom item; select a row and press Delete to remove it.")
    edited = st.data_editor(
        state["lines"], key=f"{key}_lines_{state['v']}", num_rows="dynamic", hide_index=True, width="stretch",
        column_order=["Item", "Details", "Section", "Qty", "Unit price (INR)", "GST %"],
        column_config={
            "Item": st.column_config.TextColumn(width="medium", required=True),
            "Details": st.column_config.TextColumn(width="large", help="The paragraph under the item on the quote"),
            "Section": st.column_config.SelectboxColumn(options=list(SECTION_LABELS.values()), required=True,
                                                        default=SECTION_LABELS["other"], width="small"),
            "Qty": st.column_config.NumberColumn(min_value=1, step=1, default=1, width="small"),
            "Unit price (INR)": st.column_config.NumberColumn(min_value=0, step=1000, format="₹ %d", width="small"),
            "GST %": st.column_config.NumberColumn(min_value=0, max_value=100, step=1, default=18, width="small"),
        })
    if add_items or add_bundle:
        try:
            with session_scope() as session:
                new = (lines_from_price_items(session, [(p, 1) for p in picks], basis) if add_items
                       else lines_from_bundle(session, int(bundle), basis))
        except ProcurementError as error:
            st.error(str(error))
        else:
            state["lines"] = pd.concat([edited, _lines_frame(new)], ignore_index=True)
            state["v"] += 1
            st.rerun(scope="fragment")

    try:
        lines = _frame_lines(edited)
    except ProcurementError as error:
        st.error(str(error))
        return
    totals = compute_totals(lines, Decimal(str(discount)))
    m = st.columns(4)
    m[0].metric("Subtotal", inr(totals.subtotal, decimals=False))
    m[1].metric("Discount", inr(totals.discount, decimals=False))
    m[2].metric("Total excl. GST", inr(totals.taxable, decimals=False))
    m[3].metric("Total incl. GST", inr(totals.total, decimals=False))
    with session_scope() as session:
        flags = content_flags(session, *[f"{line.description}\n{line.details or ''}" for line in lines])
    if flags:
        st.warning("Wording to check (content rules): " + flags_message(flags), icon=":material/spellcheck:")

    with st.expander("Cover page and recipient", icon=":material/description:"):
        ids = [cid for cid, _ in contacts]
        default_contact = (draft.contact_id if draft else None) or main_contact
        contact = st.selectbox("Addressed to", ids, format_func=dict(contacts).get,
                               index=ids.index(default_contact) if default_contact in ids else None,
                               placeholder="Pick a contact", key=f"{key}_contact")
        title = st.text_input("Title", value=(draft.title if draft else "") or settings.get("quote_title", ""),
                              key=f"{key}_title")
        p, q = st.columns(2)
        attention = p.text_input("Attention", value=(draft.attention if draft else "") or "", key=f"{key}_att",
                                 placeholder="Defaults to the contact's designation")
        greeting = q.text_input("Greeting", value=(draft.greeting if draft else "") or "", key=f"{key}_greet",
                                placeholder="Defaults to e.g. “Dear Dr. Rao,” or “Dear Sir/Madam,”")
        prepared = st.text_input("Prepared by", value=(draft.prepared_by if draft else "") or "", key=f"{key}_prep",
                                 placeholder="Defaults to your name and title")
        notes = st.text_input("Internal notes (not printed)", value=(draft.notes if draft else "") or "",
                              key=f"{key}_notes")

    if st.button("Save draft", type="primary", icon=":material/save:", disabled=not lines):
        new_draft = QuoteDraft(lines=lines, price_basis=basis, discount_pct=Decimal(str(discount)),
                               valid_days=int(valid_days), contact_id=contact, notes=notes, title=title,
                               attention=attention, greeting=greeting, prepared_by=prepared, ref_code=ref_code)
        try:
            with session_scope() as session:
                actor = session.get(User, user.id)
                if mode == "edit":
                    saved = update_quote(session, quote_id, new_draft, actor)
                elif mode == "revise":
                    saved = revise_quote(session, quote_id, new_draft, actor)
                else:
                    saved = create_quote(session, deal_id, new_draft, actor)
                label = f"{saved.number} v{saved.version}"
        except (ProcurementError, PermissionError) as error:
            st.error(str(error))
            return
        del st.session_state[key]
        _flash(f"Quote {label} saved as a draft. Download the Word file, check it, then Mark as sent.")


# ---------------------------------------------------------------- quote rows


def quote_actions(quote_id: int, user: CurrentUser, *, show_institution: bool = False) -> None:
    """One quote: status, value, and what can be done next."""
    with session_scope() as session:
        q = session.get(Quote, quote_id)
        if q is None:
            return
        info = {"label": f"{q.number} v{q.version}", "status": q.status, "value": q.subtotal_inr - q.discount_inr,
                "total": q.total_inr, "valid": q.valid_until, "sent": q.sent_on, "deal_id": q.opportunity_id,
                "stamp": str(q.updated_at), "link": q.drive_url, "institution": q.to_institution,
                "basis": q.price_basis, "lines": len(q.lines)}
    days_left = (info["valid"] - today_ist()).days
    tags = [(info["status"].title(), STATUS_TONE.get(info["status"], "neutral")),
            f"{inr(info['value'], decimals=False)} excl. GST"]
    if info["basis"] == "partner":
        tags.append(("Partner prices", "purple"))
    if info["status"] == "sent":
        tags.append((f"Valid until {info['valid']:%d %b}" + (f" · {days_left} days left" if days_left >= 0 else ""),
                     "orange" if days_left <= 7 else "neutral"))
    html_block(f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">'
               f'<b>{esc(info["label"])}</b>'
               + (f'<span class="qa-muted">· {esc(info["institution"])}</span>' if show_institution else "")
               + f'{chips(tags)}</div>')
    can = user.can("member")
    with st.container(horizontal=True, gap="small"):
        data, name = _docx(quote_id, info["stamp"])
        st.download_button("Word file", data=data, file_name=name, icon=":material/download:",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           key=f"qd_{quote_id}", on_click="ignore")
        if info["status"] == "draft":
            if st.button("Edit", key=f"qe_{quote_id}", icon=":material/edit:", disabled=not can):
                quote_dialog(info["deal_id"], user, quote_id, "edit")
            with st.popover("Mark as sent", icon=":material/send:", type="primary", disabled=not can):
                st.caption("Logs the email on the timeline, moves the lead to Sent Quotation and plans a reminder "
                           "before the quote expires. An older version of the same quote is marked superseded.")
                if st.button("Yes, it's sent", type="primary", key=f"qs_{quote_id}"):
                    with session_scope() as session:
                        messages = mark_quote_sent(session, quote_id, session.get(User, user.id))
                    _flash(" ".join(messages))
        if info["status"] in ("sent", "expired"):
            if st.button("Revise", key=f"qr_{quote_id}", icon=":material/difference:", disabled=not can):
                quote_dialog(info["deal_id"], user, quote_id, "revise")
            with st.popover("PO received", icon=":material/task_alt:", disabled=not can):
                po = st.date_input("PO date", value=today_ist(), format="DD/MM/YYYY", key=f"qpo_{quote_id}")
                st.caption("Marks the deal won, moves the lead to the won stage and plans delivery follow-ups "
                           "(each product's delivery time).")
                if st.button("Record PO", type="primary", key=f"qa_{quote_id}"):
                    with session_scope() as session:
                        messages = accept_quote(session, quote_id, po, session.get(User, user.id))
                    _flash(" ".join(messages))
        if info["status"] in ("draft", "sent"):
            with st.popover("Cancel", icon=":material/block:", disabled=not can):
                if st.button("Cancel this quote", key=f"qc_{quote_id}"):
                    with session_scope() as session:
                        cancel_quote(session, quote_id, session.get(User, user.id))
                    _flash("Quote cancelled.")
        with st.popover("Drive copy", icon=":material/add_link:"):
            if info["link"]:
                st.link_button("Open the Drive copy", info["link"], icon=":material/open_in_new:")
            url = st.text_input("Link to the sent PDF in Google Drive", value=info["link"] or "",
                                key=f"ql_{quote_id}")
            if st.button("Save link", key=f"qls_{quote_id}", disabled=not can):
                try:
                    with session_scope() as session:
                        set_quote_link(session, quote_id, url, session.get(User, user.id))
                except ProcurementError as error:
                    st.error(str(error))
                else:
                    _flash("Link saved.")


def render_deals(lead_id: int, user: CurrentUser) -> None:
    with session_scope() as session:
        deals = [{"id": d.id, "title": d.title, "status": d.status, "value": d.value_inr, "route": d.buying_route,
                  "funding": d.funding_source, "close": d.expected_close, "po": d.po_date, "q4": d.budget_window_flag,
                  "products": [(i.product.name, i.quantity, i.expected_delivery_on) for i in d.line_items],
                  "quotes": [q.id for q in quotes_for_deal(session, d.id)]} for d in deals_for_lead(session, lead_id)]
    with st.container(horizontal=True, gap="small"):
        if st.button("New deal", icon=":material/add_business:", type="primary", disabled=not user.can("member"),
                     key=f"deal_new_{lead_id}"):
            deal_dialog(lead_id, user)
    if not deals:
        st.caption("No deals yet. Create one when they ask for a quotation — it holds the products, buying route, "
                   "document checklist and quotes.")
        return
    for deal in deals:
        with card(f"deal-{deal['id']}"):
            tags = [(deal["status"].title(), STATUS_TONE.get(deal["status"], "neutral"))]
            if deal["value"] is not None:
                tags.append(f"{inr(deal['value'], decimals=False)} excl. GST")
            if deal["q4"] and deal["status"] == "open":
                tags.append(("Q4 budget window", "orange"))
            tags += [deal["route"] or "", deal["funding"] or ""]
            if deal["close"]:
                tags.append(f"Close by {deal['close']:%d %b %Y}")
            html_block(f'<div class="qa-row-title">{esc(deal["title"])}</div><div style="margin-top:6px">'
                       f'{chips(tags)}</div>')
            if deal["products"]:
                lines = [f"{name} × {qty}" + (f" — delivery due {due:%d %b %Y}" if due else "")
                         for name, qty, due in deal["products"]]
                st.caption(" · ".join(lines))
            with st.container(horizontal=True, gap="small"):
                if st.button("Edit deal", key=f"deal_edit_{deal['id']}", icon=":material/edit:",
                             disabled=not user.can("member")):
                    deal_dialog(lead_id, user, deal["id"])
                if deal["status"] == "open" and st.button("New quote", key=f"deal_q_{deal['id']}",
                                                          icon=":material/request_quote:", type="primary",
                                                          disabled=not user.can("member")):
                    quote_dialog(deal["id"], user)
            if deal["quotes"]:
                st.markdown("**Quotes**")
                for qid in deal["quotes"]:
                    with st.container(border=True, key=f"qa-card-plain-q{qid}"):
                        quote_actions(qid, user)
            _checklist(deal["id"], user)


__all__ = ["SECTION_ORDER", "deal_dialog", "quote_actions", "quote_dialog", "render_deals"]
