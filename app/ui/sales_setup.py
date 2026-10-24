"""Configuration → Sales setup: price list, bundles, quotation settings and template, procurement checklists, and the
content-check word list. Everyone can see these; only an admin can change them."""

from __future__ import annotations

import base64
import io
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import PriceItem, Product, User
from app.services.lead_queries import frame_to_xlsx
from app.services.library import LibraryError, flagged_frame, save_flagged
from app.services.procurement import (
    PRICE_COLUMNS,
    ProcurementError,
    bundles,
    delete_bundle,
    get_settings,
    options,
    price_items_frame,
    price_template,
    rules_frame,
    save_bundle,
    save_price_items,
    save_rules,
    save_settings,
)
from app.services.quote_docx import TEMPLATE_PATH, TEMPLATE_SETTING, TemplateError, check_template, template_bytes
from app.ui.common import CurrentUser

FLASH = "edit_flash"
MAX_UPLOAD = 5 * 1024 * 1024


def _done(message: str, kind: str = "success", details: list[str] | None = None) -> None:
    st.session_state[FLASH] = (kind, message, details or [])
    st.cache_data.clear()
    st.rerun()


def _actor(session) -> User:
    return session.get(User, st.session_state["current_user"].id) if "current_user" in st.session_state else None


# ---------------------------------------------------------------- price list


def _price_list(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        frame = price_items_frame(session)
        products = [p.name for p in session.scalars(select(Product).order_by(Product.sort_order))]
    st.caption("Each row is one line a quotation can carry. **Name** is the bold title on the quote and **Details** "
               "the text under it. Prices are in INR, excluding GST. Untick **Active** instead of deleting, so older "
               "quotes keep their history. Items without a product (installation, training…) print under the third "
               "section.")
    edited = st.data_editor(
        frame, key="price_editor", hide_index=True, width="stretch", num_rows="dynamic" if admin else "fixed",
        disabled=not admin or ["id"],
        column_order=PRICE_COLUMNS,
        column_config={
            "Name": st.column_config.TextColumn(width="large", required=True),
            "Details": st.column_config.TextColumn(width="large"),
            "Product": st.column_config.SelectboxColumn(options=["", *products]),
            "Institute price (INR)": st.column_config.NumberColumn(min_value=0, step=1000, format="₹%d"),
            "Partner price (INR)": st.column_config.NumberColumn(min_value=0, step=1000, format="₹%d"),
            "GST %": st.column_config.NumberColumn(min_value=0, max_value=100, step=1, default=18),
            "Valid from": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Valid to": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Active": st.column_config.CheckboxColumn(default=True),
        },
    )
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        if admin and st.button("Save price list", type="primary", icon=":material/save:", key="price_save"):
            try:
                with session_scope() as session:
                    added, updated = save_price_items(session, edited, _actor(session))
            except (ProcurementError, PermissionError) as error:
                st.error(str(error))
            else:
                _done(f"Price list saved — {added} added, {updated} updated.")
        stamp = date.today().isoformat()
        st.download_button("Download price list (Excel)", data=frame_to_xlsx(frame[PRICE_COLUMNS], "Price list"),
                           file_name=f"price-list-{stamp}.xlsx", icon=":material/download:", key="price_dl")
        st.download_button("Empty template (Excel)", data=frame_to_xlsx(price_template(), "Price list"),
                           file_name="price-list-template.xlsx", icon=":material/table_view:", key="price_tpl")
    if admin:
        with st.expander("Import from Excel", icon=":material/upload_file:"):
            st.caption("Use the template's columns. Rows are matched by Name: existing items are updated, new ones "
                       "added. Nothing is saved until you click Import.")
            upload = st.file_uploader("Price list (.xlsx)", type=["xlsx"], key="price_upload")
            if upload is not None:
                if upload.size > MAX_UPLOAD:
                    st.error("That file is larger than 5 MB.")
                    return
                try:
                    sheet = pd.read_excel(io.BytesIO(upload.getvalue()))
                except Exception:  # noqa: BLE001 — any unreadable workbook gets the same message
                    st.error("Couldn't read that workbook. Save it as .xlsx from Excel or Google Sheets and try again.")
                    return
                missing = [c for c in ("Name", "Institute price (INR)") if c not in sheet.columns]
                if missing:
                    st.error("Missing column(s): " + ", ".join(missing) + ". Start from the template.")
                    return
                st.dataframe(sheet, hide_index=True, width="stretch")
                if st.button(f"Import {len(sheet)} row(s)", type="primary", key="price_import"):
                    try:
                        with session_scope() as session:
                            added, updated = save_price_items(session, sheet, _actor(session))
                    except (ProcurementError, PermissionError) as error:
                        st.error(str(error))
                    else:
                        _done(f"Imported — {added} added, {updated} updated.")


# ---------------------------------------------------------------- bundles


def _bundles(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        rows = [(b.id, b.name, b.phase or "", b.description or "",
                 [(i.price_item_id, i.price_item.name, i.quantity) for i in b.items]) for b in bundles(session)]
        items = [(p.id, p.name) for p in session.scalars(select(PriceItem).where(PriceItem.is_active.is_(True))
                                                        .order_by(PriceItem.sort_order, PriceItem.name))]
    st.caption("A bundle is a ready set of price-list items — e.g. *Starter simulation lab* — that a quote can "
               "add in one click.")
    if not rows:
        st.caption("No bundles yet.")
    for bid, name, phase, description, contents in rows:
        with st.container(border=True):
            head, actions = st.columns([4, 1.4], vertical_alignment="center")
            head.markdown(f"**{name}**" + (f" · {phase}" if phase else ""))
            head.caption("; ".join(f"{qty} × {item}" for _, item, qty in contents) or "Empty")
            if admin and actions.button("Remove", key=f"bundle_del_{bid}", type="tertiary"):
                with session_scope() as session:
                    delete_bundle(session, bid, _actor(session))
                _done(f"Bundle '{name}' removed.")
    if not admin:
        return
    if not items:
        st.info("Add items to the price list first.")
        return
    with st.expander("Add a bundle", icon=":material/add:"):
        with st.form("bundle_form", clear_on_submit=True):
            a, b = st.columns([2, 1])
            name = a.text_input("Name", placeholder="e.g. Starter simulation lab")
            phase = b.text_input("Phase (optional)", placeholder="e.g. Phase 1")
            description = st.text_input("Description (optional)")
            picks = st.data_editor(pd.DataFrame({"Item": pd.Series(dtype="str"), "Quantity": pd.Series(dtype="int")}),
                                   num_rows="dynamic", hide_index=True, width="stretch", key="bundle_items",
                                   column_config={
                                       "Item": st.column_config.SelectboxColumn(options=[n for _, n in items],
                                                                                required=True, width="large"),
                                       "Quantity": st.column_config.NumberColumn(min_value=1, step=1, default=1)})
            if st.form_submit_button("Save bundle", type="primary"):
                by_name = {n: i for i, n in items}
                chosen = [(by_name[r["Item"]], int(r["Quantity"] or 1)) for r in picks.to_dict("records")
                          if r.get("Item") in by_name]
                try:
                    with session_scope() as session:
                        save_bundle(session, name=name, phase=phase, description=description, items=chosen,
                                    user=_actor(session))
                except (ProcurementError, PermissionError) as error:
                    st.error(str(error))
                else:
                    _done(f"Bundle '{name}' saved.")


# ---------------------------------------------------------------- quotation settings + template


def _quotation(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        settings = get_settings(session)
        team = [(u.name, u.email.lower()) for u in session.scalars(select(User).where(User.is_active.is_(True))
                                                                    .order_by(User.id))]
    st.caption("Letterhead, payment terms, support rates and terms & conditions are in the **Word template** below — "
               "edit them in Word. These settings fill the parts that change per quote.")
    with st.form("quote_settings"):
        a, b, c = st.columns(3)
        prefix = a.text_input("Reference prefix", value=settings.get("quote_prefix", "DEMO"),
                              help="References look like DEMO/KIT/2026-27/001 — prefix / institution code / "
                                   "financial year / number.")
        validity = b.number_input("Valid for (days)", min_value=1, max_value=365,
                                  value=int(settings.get("quote_validity_days") or 30))
        remind = c.number_input("Reminder before expiry (days)", min_value=1, max_value=60,
                                value=int(settings.get("quote_expiry_reminder_days") or 5))
        summary = st.toggle("Add a summary table with GST (not in your quotation — off by default)",
                            value=settings.get("quote_show_summary", "yes").lower() in ("yes", "true", "1"))
        values: dict[str, str] = {}
        for key, label in (("software", "Section for simulators (software)"),
                           ("hardware", "Section for experimentation kits (hardware)"),
                           ("other", "Section for everything else (services, items without a product)")):
            st.markdown(f"**{label}**")
            values[f"section_{key}_title"] = st.text_input("Title", value=settings.get(f"section_{key}_title", ""),
                                                           key=f"sec_t_{key}")
            values[f"section_{key}_intro"] = st.text_area("Introduction", value=settings.get(f"section_{key}_intro", ""),
                                                          key=f"sec_i_{key}", height=80)
        st.markdown("**Job titles** (printed after the name in *Prepared by* and under the signature)")
        for name, email in team:
            values[f"title:{email}"] = st.text_input(name, value=settings.get(f"title:{email}", ""),
                                                     key=f"title_{email}")
        if st.form_submit_button("Save quotation settings", type="primary", disabled=not admin):
            values |= {"quote_prefix": prefix.strip().upper() or "DEMO", "quote_validity_days": str(int(validity)),
                       "quote_expiry_reminder_days": str(int(remind)),
                       "quote_show_summary": "yes" if summary else "no"}
            with session_scope() as session:
                n = save_settings(session, values, _actor(session))
            _done(f"Quotation settings saved ({n} change(s)).")

    st.markdown("**Word template**")
    custom = bool(settings.get(TEMPLATE_SETTING))
    st.caption(("Using your uploaded template." if custom else "Using the built-in Demo template.")
               + " To change wording or terms: download it, edit in Word, keep the {fields} and the model item table, "
                 "and upload it again.")
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.download_button("Download current template", data=template_bytes(settings),
                           file_name="quotation-template.docx", icon=":material/description:", key="tpl_dl",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if admin and custom and st.button("Go back to the built-in template", key="tpl_reset", type="tertiary"):
            with session_scope() as session:
                save_settings(session, {TEMPLATE_SETTING: ""}, _actor(session))
            _done("Back to the built-in template.")
    if admin:
        upload = st.file_uploader("Upload a new template (.docx, up to 3 MB)", type=["docx"], key="tpl_upload")
        if upload is not None:
            data = upload.getvalue()
            try:
                fields = check_template(data)
            except TemplateError as error:
                st.error(str(error))
            else:
                st.success("Template looks good. Fields found: " + ", ".join(fields))
                if st.button("Use this template", type="primary", key="tpl_use"):
                    with session_scope() as session:
                        save_settings(session, {TEMPLATE_SETTING: base64.b64encode(data).decode()}, _actor(session))
                    _done("New quotation template in use.")
    if not TEMPLATE_PATH.exists():
        st.warning("The built-in template file is missing from assets/.")


# ---------------------------------------------------------------- checklists


def _checklists(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        frame = rules_frame(session)
        routes = options(session, "buying_route")
    st.caption("Documents each buying route needs. A deal's checklist is built from these when its route is set. "
               "'Any' rows apply to every route. Demo is not on GeM, so government institutions use the PAC "
               "route.")
    edited = st.data_editor(
        frame, key="rules_editor", hide_index=True, width="stretch", num_rows="dynamic" if admin else "fixed",
        disabled=not admin or ["id"], column_order=["Buying route", "Document needed", "Only for reseller deals",
                                                    "Active"],
        column_config={
            "Buying route": st.column_config.SelectboxColumn(options=["Any", *routes], required=True),
            "Document needed": st.column_config.TextColumn(width="large", required=True),
            "Only for reseller deals": st.column_config.CheckboxColumn(default=False),
            "Active": st.column_config.CheckboxColumn(default=True),
        },
    )
    if admin and st.button("Save checklist rules", type="primary", icon=":material/save:", key="rules_save"):
        with session_scope() as session:
            n = save_rules(session, edited, _actor(session))
        _done(f"Checklist rules saved ({n} change(s)). Existing deals update the next time they're edited.")


# ---------------------------------------------------------------- content check


def _content_check(user: CurrentUser) -> None:
    admin = user.can("admin")
    with session_scope() as session:
        frame = flagged_frame(session)
    st.caption("Words that shouldn't appear in documents, scripts or objection answers — e.g. specific hardware "
               "component names. They're flagged before anything is approved, with the generic wording to use "
               "instead.")
    edited = st.data_editor(
        frame, key="flagged_editor", hide_index=True, width="stretch", num_rows="dynamic" if admin else "fixed",
        disabled=not admin or ["id"], column_order=["Word or phrase", "Use instead", "Active"],
        column_config={"Word or phrase": st.column_config.TextColumn(required=True),
                       "Active": st.column_config.CheckboxColumn(default=True)},
    )
    if admin and st.button("Save word list", type="primary", icon=":material/save:", key="flagged_save"):
        try:
            with session_scope() as session:
                n = save_flagged(session, edited, _actor(session))
        except (LibraryError, PermissionError) as error:
            st.error(str(error))
        else:
            _done(f"Word list saved ({n} change(s)).")


def render_sales_setup(user: CurrentUser) -> None:
    tabs = st.tabs(["Price list", "Bundles", "Quotation", "Procurement checklists", "Content check"])
    for tab, render in zip(tabs, (_price_list, _bundles, _quotation, _checklists, _content_check)):
        with tab:
            render(user)
    if not user.can("admin"):
        st.caption("Only an admin can change these.")
