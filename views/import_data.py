"""Import the team's Google Sheets: check first (nothing saved), then import, with undo."""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.db import session_scope
from app.models import Track, User
from app.services.importing.columns import FIELDS_BY_KIND
from app.services.importing.readers import UploadError, combine_sheets, read_pasted, read_upload, same_columns
from app.services.importing.runner import (
    COLLECTION_METHODS,
    DEFAULT_LEAD_SOURCE,
    KIND_LABELS,
    ImportOptions,
    ImportReport,
    run_import,
)
from app.services.importing.undo import undo_import
from app.services.lead_queries import import_history
from app.ui.common import require_user
from app.ui.layout import page_header, section_header

user = require_user()
page_header(
    "Import from Google Sheets",
    "In Google Sheets: File → Download → Microsoft Excel (.xlsx). Upload it here, check it, then import. "
    "Every import can be undone.",
    eyebrow="Data",
)

KIND_HELP = {
    "tracker": "One row per contact. Rows for the same organisation become one lead with several contacts.",
    "responses": "Each row is logged as a dated response on the organisation's timeline. Needs Date and Source.",
    "clients": "Each row marks the organisation as a client (Deal PO Released). With a PO date and products, "
               "delivery due dates and follow-ups are added too.",
    "mini": "Your Mini products tracker: one row per person (or organisation). Goes into the Mini products track; "
            "people without an organisation are saved as individuals.",
    "colleges": "A list of organisations — an AISHE college list, a list of companies, schools or labs. They "
                "appear on the Organisations page (not leads yet). Add an Industry column to file each one; "
                "overlapping lists are fine, they're matched by AISHE code, website domain or name.",
}
DEFAULT_METHOD = {
    "tracker": "Organisation website / public directory",
    "responses": "They contacted us (enquiry / reply)",
    "clients": "Existing customer (gave details for the purchase)",
    "mini": "They contacted us (enquiry / reply)",
    "colleges": "Organisation website / public directory",
}

with session_scope() as session:
    tracks = [(t.id, t.name, t.code) for t in session.scalars(select(Track).where(Track.is_active.is_(True))
                                                            .order_by(Track.sort_order))]
    team = [(u.id, u.name) for u in session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id))]

with st.container(border=True):
    section_header("1 · What are you importing?")
    kind = st.radio("Sheet type", list(KIND_LABELS), format_func=KIND_LABELS.get, horizontal=True,
                    label_visibility="collapsed", key="import_kind")
    st.caption(KIND_HELP[kind])

    with st.expander("Columns this import understands"):
        st.dataframe(
            pd.DataFrame(
                [{"Column": f.label, "Required": "Yes" if f.required else "", "Also recognised as": ", ".join(f.aliases)}
                 for f in FIELDS_BY_KIND[kind]]
            ),
            hide_index=True,
            width="stretch",
        )

    left, right = st.columns(2)
    track_id: int | None = None
    lead_source: str | None = None
    default_owner_id: int | None = None
    list_name: str | None = None
    if kind in ("tracker", "responses", "clients"):
        track_labels = {tid: name for tid, name, _ in tracks}
        track_id = left.selectbox("Track", list(track_labels), format_func=track_labels.get)
        code = next(code for tid, _, code in tracks if tid == track_id)
        if kind in ("tracker", "clients"):
            lead_source = right.text_input(
                "Lead source", value=DEFAULT_LEAD_SOURCE.get(code, "") if kind == "tracker" else "Client",
                help="Recorded on new leads, e.g. 'Fresh' or 'Referral'.",
            )
    if kind == "mini":
        left.caption("Track: **Mini products** (set automatically)")
        lead_source = right.text_input("Lead source", value="Mini products")
    method = DEFAULT_METHOD[kind]
    already_verified = False
    delivery_tasks = True
    fill_down = False
    if kind != "colleges":
        methods = list(COLLECTION_METHODS)
        method = left.selectbox(
            "How were these contacts collected?", methods, index=methods.index(DEFAULT_METHOD[kind]),
            help="Saved in the consent log for every new contact (needed under the DPDP Act).",
        )
        owner_labels = {0: "— nobody —", **{uid: name for uid, name in team}}
        default_owner = next((uid for uid, name in team if kind == "mini" and name.lower().startswith("riya")), 0)
        default_owner_id = right.selectbox(
            "Owner when 'Owner' is blank", list(owner_labels), format_func=owner_labels.get,
            index=list(owner_labels).index(default_owner),
        ) or None
    if kind == "tracker":
        already_verified = right.checkbox(
            "These contacts are already verified", value=False,
            help="Tick only if the emails in this sheet have been checked. Only verified contacts can enter email cadences.",
        )
        fill_down = left.checkbox(
            "Organisation name is only on the first row of each group (fill it down)", value=False,
            help="For sheets like 'Tier2&3 State Uni' where the next rows under a university are its other contacts.",
        )
    if kind == "clients":
        delivery_tasks = right.checkbox(
            "Schedule delivery follow-ups", value=True,
            help="Adds a dated follow-up for each product still to be delivered (each product's delivery time after the PO).",
        )

section_header("2 · Add the rows")
mode = st.radio("How", ["Upload a file", "Paste rows"], horizontal=True, label_visibility="collapsed",
                key="import_mode")


@st.cache_data(show_spinner="Reading the file…", max_entries=3)
def _read(data: bytes, name: str):
    return read_upload(data, name)


source_name: str | None = None
data: bytes = b""
sheets: list = []
if mode == "Upload a file":
    upload = st.file_uploader("Upload the sheet", type=["xlsx", "csv"], label_visibility="collapsed")
    if upload is not None:
        source_name, data = upload.name, upload.getvalue()
        try:
            sheets = _read(data, upload.name)
        except UploadError as error:
            st.error(str(error))
            st.stop()
else:
    labels = [f.label for f in FIELDS_BY_KIND[kind]]
    with st.expander("Column headers for this sheet type (copy into your AI prompt)", icon=":material/content_copy:"):
        st.caption("Ask the AI for a table with exactly these columns. Required: "
                   + ", ".join(f.label for f in FIELDS_BY_KIND[kind] if f.required) + ".")
        st.code(" | ".join(labels), language=None)
    pasted = st.text_area(
        "Paste rows", height=220, label_visibility="collapsed", key=f"paste_{kind}",
        placeholder="Select the cells in Google Sheets / Excel — including the header row — copy, and paste here.\n"
                    "A table from a Claude or ChatGPT answer works too.\n\nThen press Ctrl+Enter or click outside the box.",
    )
    if pasted.strip():
        source_name, data = "Pasted rows", pasted.encode()
        try:
            sheets = [read_pasted(pasted)]
        except UploadError as error:
            st.error(str(error))
            st.stop()

if sheets:
    sheet = sheets[0]
    if len(sheets) > 1:
        # Offer 'all tabs like this one' when several tabs share the same columns (e.g. Colleges 1…94).
        choices: dict[str, list] = {}
        for candidate in sheets:
            group = [s for s in sheets if same_columns(s, candidate)]
            if len(group) > 1 and group[0] is candidate:
                choices[f"All {len(group)} tabs with these columns ({candidate.name}, {group[1].name}…)"] = group
        for candidate in sheets:
            choices[candidate.name] = [candidate]
        chosen = st.selectbox("Which tab?", list(choices))
        group = choices[chosen]
        try:
            sheet = group[0] if len(group) == 1 else combine_sheets(group)
        except UploadError as error:
            st.error(str(error))
            st.stop()
    st.caption(f"{len(sheet.frame):,} rows found in '{sheet.name}'.")
    if kind == "colleges":
        stem = source_name.rsplit(".", 1)[0]
        list_name = st.text_input(
            "List name (tags every organisation in this upload)", value=stem,
            help="Lets you filter the Organisations page by list later, e.g. 'Rural colleges' or 'Telecom companies'.",
        ).strip() or None

    options = ImportOptions(
        kind=kind,
        track_id=track_id,
        lead_source=(lead_source or None),
        collection_method=method,
        already_verified=already_verified,
        create_delivery_tasks=delivery_tasks,
        fill_down=fill_down,
        default_owner_id=default_owner_id,
        list_name=list_name,
        file_name=source_name,
        sheet_name=sheet.name if len(sheets) > 1 else None,
    )
    # Any change to the file or the options invalidates an earlier check.
    fingerprint = hashlib.sha256(data + repr(options).encode()).hexdigest()

    def show_report(report: ImportReport, saved: bool) -> None:
        mapping = report.mapping
        if mapping is not None:
            found = ", ".join(f"{k} ← '{v}'" for k, v in mapping.columns.items())
            st.caption(f"Columns used: {found}")
            if mapping.unused_headers:
                st.caption("Ignored columns: " + ", ".join(mapping.unused_headers))
        summary = report.summary()
        if summary:
            cols = st.columns(min(4, len(summary)))
            for i, (label, value) in enumerate(summary.items()):
                cols[i % len(cols)].metric(label, f"{value:,}")
        verb = "imported" if saved else "can be imported"
        st.write(f"**{report.rows_imported:,} of {report.rows_total:,} rows {verb}.**")
        if report.errors:
            with st.expander(f"⛔ {len(report.errors)} rows skipped", expanded=not saved):
                st.dataframe(pd.DataFrame([{"Row": "" if e.row is None else str(e.row), "Reason": e.message} for e in report.errors]),
                             hide_index=True, width="stretch")
        if report.warnings:
            with st.expander(f"⚠️ {len(report.warnings)} things to check"):
                st.dataframe(pd.DataFrame([{"Row": "" if w.row is None else str(w.row), "Note": w.message} for w in report.warnings]),
                             hide_index=True, width="stretch")
        if report.outcomes:
            with st.expander("Row by row"):
                st.dataframe(pd.DataFrame(report.outcomes[:2000]), hide_index=True, width="stretch")

    stored = st.session_state.get("import_check")
    if stored and stored["fingerprint"] != fingerprint:
        stored = None

    if not user.can("member"):
        st.warning("Viewers can check a file but not import it.")

    if st.button("Check the file (nothing is saved)", type="secondary", icon=":material/fact_check:"):
        with st.spinner("Checking every row against the database…"):
            with session_scope() as session:
                db_user = session.get(User, user.id)
                report = run_import(session, sheet.frame, options, db_user, dry_run=True)
                session.rollback()
        st.session_state["import_check"] = {"fingerprint": fingerprint, "report": report}
        stored = st.session_state["import_check"]

    if stored:
        report: ImportReport = stored["report"]
        section_header("3 · Check result")
        show_report(report, saved=False)
        can_import = report.rows_imported > 0 and user.can("member") and not (
            report.mapping is not None and not report.mapping.ok
        )
        if can_import and st.button(f"Import {report.rows_imported:,} rows", type="primary", icon=":material/cloud_upload:"):
            with st.spinner("Importing…"):
                with session_scope() as session:
                    db_user = session.get(User, user.id)
                    result = run_import(session, sheet.frame, options, db_user, dry_run=False)
            st.session_state.pop("import_check", None)
            st.session_state["import_result"] = result
            if result.batch_id:
                st.session_state.pop(f"paste_{kind}", None)  # empty the paste box so rows aren't imported twice
            st.cache_data.clear()  # so the Leads and Organisations pages show the new rows straight away
            st.rerun()

    finished: ImportReport | None = st.session_state.pop("import_result", None)
    if finished is not None:
        section_header("Import result")
        if finished.batch_id:
            st.success(f"Imported. Saved as import #{finished.batch_id} — you can undo it below.")
        else:
            st.error("Nothing was imported.")
        show_report(finished, saved=True)

st.divider()
section_header("Import history")
flash = st.session_state.pop("undo_message", None)
if flash:
    st.success(flash)
with session_scope() as session:
    history = import_history(session)
if history.empty:
    st.caption("No imports yet.")
else:
    st.dataframe(history, hide_index=True, width="stretch")
    if user.can("admin"):
        open_batches = history[history["Undone"].isna()]["ID"].tolist()
        if open_batches:
            with st.expander("Undo an import"):
                st.caption(
                    "Deletes the organisations, contacts, leads, responses and deals that import created — "
                    "unless something added later depends on them. Blanks it filled in and stage changes stay."
                )
                batch_id = st.selectbox("Import to undo", open_batches, format_func=lambda i: f"#{i}")
                sure = st.checkbox(f"Yes, undo import #{batch_id}")
                if st.button("Undo", disabled=not sure):
                    with session_scope() as session:
                        result = undo_import(session, int(batch_id), session.get(User, user.id))
                    st.cache_data.clear()
                    deleted = ", ".join(f"{k}: {v}" for k, v in result.deleted.items()) or "nothing"
                    kept = ", ".join(f"{k}: {v}" for k, v in result.kept.items() if v)
                    st.session_state["undo_message"] = (
                        f"Import #{batch_id} undone. Deleted {deleted}."
                        + (f" Kept (in use elsewhere): {kept}." if kept else "")
                    )
                    st.rerun()
