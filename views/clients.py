"""Clients (installed base): every won deal from delivery to installation, acceptance, product training, warranty,
AMC and the next purchase — with reminders on Today so renewals and upsell don't slip."""

from __future__ import annotations


import pandas as pd
import streamlit as st

from app.db import session_scope
from app.services.cadence import today_ist
from app.models import Installation, User
from app.models.partners import AMC_STATUSES
from app.services.lead_queries import frame_to_xlsx
from app.services.money import inr
from app.services.postsale import PostSaleError, installations_frame, save_installation, suggested_amc
from app.ui.common import require_user
from app.ui.components import card, chips, esc, html_block
from app.ui.layout import empty_state, page_header

user = require_user()
FLASH = "edit_flash"


@st.cache_data(ttl=60, show_spinner=False)
def _clients() -> pd.DataFrame:
    with session_scope() as session:
        return installations_frame(session)


def _record(installation_id: int) -> dict | None:
    with session_scope() as session:
        inst = session.get(Installation, installation_id)
        if inst is None:
            return None
        values = {k: getattr(inst, k) for k in (
            "delivered_on", "installed_on", "accepted_on", "training_on", "training_done", "warranty_months",
            "warranty_end", "amc_status", "amc_start", "amc_end", "amc_value_inr", "next_phase", "upsell_on",
            "reference_ok", "case_study_ok", "notes", "opportunity_id")}
        values["amc"] = suggested_amc(session, inst.opportunity_id)
        return values


def client_panel(row: dict) -> None:
    iid = int(row["id"])
    r = _record(iid)
    if r is None:
        st.warning("That client record no longer exists.")
        return
    with card(f"client-{iid}"):
        head, link = st.columns([4, 1.3], vertical_alignment="top")
        tone = "orange" if row["Overdue"] else "blue"
        po = ("PO " + row["PO date"].strftime("%d %b %Y")) if pd.notna(row["PO date"]) else ""
        tags = chips([row["Deal"], inr(row["Value (₹)"], decimals=False), po, ("Next: " + row["Next"], tone)])
        with head:
            html_block(f'<h3 style="margin:0;padding:0">{esc(row["Client"])}</h3>'
                       f'<div style="margin-top:4px">{tags}</div>'
                       + (f'<div class="qa-small qa-muted" style="margin-top:4px">{esc(row["Products"])}</div>'
                          if row["Products"] else ""))
        link.page_link("views/leads.py", label="Open the lead", icon=":material/open_in_new:",
                       query_params={"lead": int(row["lead_id"])})
    editable = user.can("member")
    with st.form(f"client_form_{iid}", border=False):
        st.markdown("**Delivery and installation**")
        a, b, c = st.columns(3)
        delivered = a.date_input("Delivered on", value=r["delivered_on"], format="DD/MM/YYYY")
        installed = b.date_input("Installed on", value=r["installed_on"], format="DD/MM/YYYY")
        accepted = c.date_input("Accepted on", value=r["accepted_on"], format="DD/MM/YYYY",
                                help="The acceptance / installation certificate date — the warranty starts here.")
        a, b, c = st.columns(3)
        training = a.date_input("Product training on", value=r["training_on"], format="DD/MM/YYYY",
                                help="A reminder goes to the lead owner on that day.")
        trained = b.checkbox("Training done", value=r["training_done"])
        months = c.number_input("Warranty (months)", min_value=0, max_value=120, value=int(r["warranty_months"]),
                                step=6)
        if r["warranty_end"]:
            st.caption(f"Warranty ends **{r['warranty_end']:%d %b %Y}** (reminder 60 days before, unless an AMC "
                       "is active).")
        st.markdown("**AMC**")
        a, b, c, d = st.columns(4)
        amc_status = a.selectbox("AMC status", AMC_STATUSES, format_func=str.title,
                                 index=AMC_STATUSES.index(r["amc_status"]))
        amc_start = b.date_input("AMC from", value=r["amc_start"], format="DD/MM/YYYY")
        amc_end = c.date_input("AMC until", value=r["amc_end"], format="DD/MM/YYYY")
        amc_value = d.text_input("AMC value (₹/year)", value="" if r["amc_value_inr"] is None
                                 else f"{r['amc_value_inr']:.0f}")
        st.markdown("**What's next**")
        a, b = st.columns([2, 1])
        next_phase = a.text_input("Next phase / upsell", value=r["next_phase"] or "",
                                  placeholder="e.g. Phase 2 — experimentation kits; QSim Cluster seats")
        upsell = b.date_input("Follow up on", value=r["upsell_on"], format="DD/MM/YYYY")
        a, b = st.columns(2)
        reference = a.checkbox("Happy to be a reference", value=r["reference_ok"])
        case_study = b.checkbox("Agreed to a case study", value=r["case_study_ok"])
        notes = st.text_area("Notes", value=r["notes"] or "", height=68)
        save = st.form_submit_button("Save", type="primary", icon=":material/save:", disabled=not editable)
    if save:
        try:
            with session_scope() as session:
                planned = save_installation(session, iid, {
                    "delivered_on": delivered, "installed_on": installed, "accepted_on": accepted,
                    "training_on": training, "training_done": trained, "warranty_months": months,
                    "amc_status": amc_status, "amc_start": amc_start, "amc_end": amc_end, "amc_value_inr": amc_value,
                    "next_phase": next_phase, "upsell_on": upsell, "reference_ok": reference,
                    "case_study_ok": case_study, "notes": notes}, session.get(User, user.id))
        except (PostSaleError, PermissionError) as error:
            st.error(str(error))
        else:
            st.session_state[FLASH] = ("success", "Saved." + (" Reminders on Today: " + "; ".join(planned) + "."
                                                               if planned else ""), [])
            st.cache_data.clear()
            st.rerun()
    with st.expander("Suggested AMC (from the quotation's support terms)"):
        st.caption("Support from year 2 as a % of the deal value (excl. GST). Change the rates in Configuration → "
                   "settings (amc_rates).")
        amc = pd.DataFrame(r["amc"])
        if not amc.empty:
            amc["Suggested AMC (₹)"] = amc["Suggested AMC (₹)"].map(lambda v: inr(v, decimals=False))
        st.dataframe(amc, hide_index=True)


# ---------------------------------------------------------------- page

page_header("Clients", "Every won deal after the PO: delivery, installation, acceptance, product training, warranty, "
            "AMC and the next phase. Reminders land on Today.", eyebrow="Pipeline")
clients = _clients()
if clients.empty:
    empty_state("No clients yet. A client record starts automatically when a quotation is marked as accepted "
                "(PO received) or a lead is marked won.", page="views/quotes.py", label="Quotes")
    st.stop()

today = today_ist()
soon = pd.Timestamp(today) + pd.Timedelta(days=90)


def _within(column: str) -> pd.Series:
    values = pd.to_datetime(clients[column])
    return values.notna() & (values >= pd.Timestamp(today)) & (values <= soon)


renewals = ((_within("Warranty ends") & clients["AMC"].isin(["None", "Quoted"]))
            | (_within("AMC ends") & (clients["AMC"] == "Active")))
m = st.columns(5)
m[0].metric("Clients", len(clients), border=True)
m[1].metric("Awaiting installation", int(clients["Installed"].isna().sum()), border=True)
m[2].metric("Training pending", int((clients["Training"] != "Done").sum()), border=True)
m[3].metric("Renewals in 90 days", int(renewals.sum()), border=True,
            help="Warranties ending without an AMC, and AMCs ending.")
m[4].metric("Overdue steps", int(clients["Overdue"].sum()), border=True)

view_mode = st.radio("Show", ["All", "Needs action", "Renewals in 90 days"], horizontal=True, key="client_view")
view = clients
if view_mode == "Needs action":
    view = clients[clients["Overdue"] | (clients["Next"].isin(["Delivery", "Installation", "Acceptance",
                                                               "Product training"]))]
elif view_mode == "Renewals in 90 days":
    view = clients[renewals]
shown = view.drop(columns=["id", "opportunity_id", "lead_id"])
display = shown.assign(**{"Value (₹)": shown["Value (₹)"].map(lambda v: inr(v, decimals=False))})
picked = st.dataframe(
    display, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="clients_table",
    height=min(440, 35 * (len(shown) + 1) + 3),
    column_order=["Client", "Next", "When", "Overdue", "Products", "PO date", "Value (₹)", "Delivered", "Installed",
                  "Accepted", "Training", "Warranty ends", "AMC", "AMC ends", "Next phase", "Upsell on", "Reference"],
    column_config={c: st.column_config.DateColumn(format="DD MMM YYYY") for c in (
        "PO date", "Delivery due", "Delivered", "Installed", "Accepted", "Warranty ends", "AMC ends", "Upsell on",
        "When")} | {"Value (₹)": st.column_config.TextColumn(width="small"),
                    "Overdue": st.column_config.CheckboxColumn(width="small"),
                    "Reference": st.column_config.CheckboxColumn(width="small")})
rows = picked.selection.rows if picked and picked.selection else []
st.download_button("Clients (Excel)", data=frame_to_xlsx(shown, "Clients"), icon=":material/download:",
                   file_name=f"clients-{today.isoformat()}.xlsx")
if rows:
    client_panel(view.iloc[rows[0]].to_dict())
else:
    st.caption("Tick a client at the left edge to update its milestones.")
