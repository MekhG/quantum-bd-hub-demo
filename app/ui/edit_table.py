"""The editable table used on Leads, Colleges and Contacts: edit cells → review → save.

Why the editor's key includes a fingerprint of the rows: Streamlit remembers edits by row *position*.
If filters change the rows underneath, old edits would land on the wrong rows — a fresh key per set of
rows makes that impossible (unsaved edits are dropped instead, and the page says so).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pandas as pd
import streamlit as st

from app.db import session_scope
from app.models import User
from app.services.editing import GRID_FIELDS, EditPlan, apply_plan, plan_edits
from app.ui.common import CurrentUser

_FLASH = "edit_flash"


def show_edit_flash() -> None:
    """Shows the result of the last save (kept across the rerun that follows saving)."""
    flash = st.session_state.pop(_FLASH, None)
    if not flash:
        return
    kind, message, details, *rest = flash
    getattr(st, kind)(message)
    if details:
        label = rest[0] if rest else f"{len(details)} row(s) not saved — redo these"
        with st.expander(label, expanded=len(details) <= 10):
            for line in details:
                st.markdown(f"- {line}")


def edit_table(
    *,
    grid: str,
    frame: pd.DataFrame,
    columns: list[str],
    column_config: dict,
    label_column: str,
    user: CurrentUser,
    key: str,
    height: int | str = "auto",
    on_saved: Callable[[], None] | None = None,
) -> None:
    """`frame` must be indexed by the row id and carry a `_version` column."""
    editable = {f.column for f in GRID_FIELDS[grid]}
    shown = frame[columns]
    fingerprint = hashlib.sha1(
        (",".join(map(str, frame.index)) + "|".join(frame["_version"].astype(str))).encode()
    ).hexdigest()[:10]
    round_no = st.session_state.get(f"{key}_round", 0)
    editor_key = f"{key}_{fingerprint}_{round_no}"
    can_delete = user.can("admin")

    st.caption(
        "Edit cells directly, then **Review & save**. "
        + ("Admins can delete rows: tick the box at the left edge, then press Delete. " if can_delete else "")
        + "Changing filters drops edits that aren't saved yet."
    )
    edited = st.data_editor(
        shown,
        key=editor_key,
        hide_index=True,
        width="stretch",
        height=height,
        num_rows="delete" if can_delete else "fixed",
        disabled=[c for c in columns if c not in editable],
        column_config=column_config,
        placeholder="",  # show empty cells as blank instead of 'None'
    )
    plan = plan_edits(grid, shown, edited, label_column)
    n_fields = sum(len(e.values) for e in plan.edits)
    with st.container(horizontal=True, vertical_alignment="center"):
        label = "Review & save"
        if not plan.is_empty:
            parts = [f"{n_fields} change(s) in {len(plan.edits)} row(s)"] if plan.edits else []
            parts += [f"{len(plan.deletes)} deletion(s)"] if plan.deletes else []
            label = f"Review & save — {', '.join(parts)}"
        if st.button(label, type="primary", icon=":material/save:", disabled=plan.is_empty, key=f"{key}_save"):
            _review(plan, frame["_version"].to_dict(), user, key, on_saved)
        if st.button("Discard edits", icon=":material/undo:", disabled=plan.is_empty, key=f"{key}_discard"):
            st.session_state[f"{key}_round"] = round_no + 1
            st.rerun()


@st.dialog("Review changes", width="large")
def _review(plan: EditPlan, versions: dict[int, str], user: CurrentUser, key: str,
            on_saved: Callable[[], None] | None) -> None:
    st.dataframe(plan.describe(), hide_index=True, width="stretch")
    if plan.deletes:
        st.warning(f"{len(plan.deletes)} row(s) will be **deleted**. This is recorded in History but can't be "
                   "undone from the app.", icon=":material/delete:")
    sure = st.checkbox("Yes, delete these rows") if plan.deletes else True
    if st.button("Save", type="primary", icon=":material/check:", disabled=not sure):
        try:
            with session_scope() as session:
                result = apply_plan(session, plan, versions, session.get(User, user.id))
        except PermissionError as error:
            st.error(str(error))
            return
        saved = []
        if result.saved_rows:
            saved.append(f"{result.saved_rows} row(s) saved")
        if result.deleted:
            saved.append(f"{result.deleted} deleted")
        problems = result.conflicts + result.errors
        message = (", ".join(saved) + ".") if saved else "Nothing was saved."
        kind = "success" if saved and not problems else ("warning" if saved else "error")
        st.session_state[_FLASH] = (kind, message, problems)
        st.session_state[f"{key}_round"] = st.session_state.get(f"{key}_round", 0) + 1
        st.cache_data.clear()
        if on_saved:
            on_saved()
        st.rerun()
