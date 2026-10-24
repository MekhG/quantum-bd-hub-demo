"""Sign-in guard shared by every page."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db import session_scope
from app.config import is_demo
from app.services.auth import auth_configured, find_user, signed_in_email

_ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
_SESSION_KEY = "current_user"


@dataclass(frozen=True)
class CurrentUser:
    """A plain copy of the signed-in user, safe to keep between Streamlit reruns."""

    id: int
    name: str
    email: str
    role: str

    def can(self, role: str) -> bool:
        return _ROLE_RANK.get(self.role, -1) >= _ROLE_RANK[role]

    @property
    def first_name(self) -> str:
        return self.name.split(" ")[0] if self.name else ""


def show_connection_help(error: Exception) -> None:
    message = str(error).lower()
    if "password authentication failed" in message:
        st.error("The database refused the password. Check the password inside DATABASE_URL in your .env file.")
    elif "does not exist" in message or "undefinedtable" in message or "undefinedcolumn" in message:
        st.error("The database needs updating. In the terminal run:  alembic upgrade head")
    else:
        st.error("Couldn't reach the database. Check DATABASE_URL in .env and your internet connection.")
    with st.expander("Technical details"):
        st.code(str(error))


def sign_in_page() -> None:
    """Shown online to anyone not signed in yet. Google checks who they are; the team list decides what they see."""
    from app.ui.layout import LOGO

    _, middle, _ = st.columns([1, 1.4, 1])
    with middle, st.container(border=True):
        st.image(str(LOGO), width=170)
        st.title("BD & Marketing Hub", anchor=False)
        st.caption("Sign in with your work Google account. Only people on the team list can open the hub.")
        st.button("Sign in with Google", on_click=st.login, type="primary", icon=":material/login:",
                  width="stretch")
    st.stop()


def authenticate() -> CurrentUser:
    """Looks up the signed-in person once per run (called by streamlit_app.py before any page draws)."""
    email = signed_in_email()
    if not email:
        if is_demo():
            from app.demo.personas import welcome_page

            welcome_page()
        if auth_configured():
            sign_in_page()
        st.warning(
            "No signed-in user. When running on your laptop, add a line "
            "`DEV_USER_EMAIL=you@example.com` to .env with an email from the team list."
        )
        st.stop()
    try:
        with session_scope() as session:
            user = find_user(session, email)
            current = CurrentUser(user.id, user.name, user.email, user.role) if user else None
    except (OperationalError, ProgrammingError) as error:
        show_connection_help(error)
        st.stop()
    except RuntimeError as error:  # DATABASE_URL missing
        st.error(str(error))
        st.stop()

    if current is None and is_demo():
        from app.demo.personas import switch_persona

        switch_persona()  # the demo data was rebuilt and this persona is gone: back to the welcome page
        st.rerun()
    if current is None:
        st.error(f"{email} isn't registered as a team member. Ask the admin to add you, or sign in with your "
                 "work email.")
        if auth_configured():
            st.button("Sign out", on_click=st.logout, icon=":material/logout:")
        st.stop()
    st.session_state[_SESSION_KEY] = current
    return current


def require_user(min_role: str = "viewer") -> CurrentUser:
    """Returns the signed-in person, stopping the page if their role is too low."""
    current: CurrentUser | None = st.session_state.get(_SESSION_KEY)
    if current is None:
        current = authenticate()
    if not current.can(min_role):
        st.error(f"This page needs {min_role} access. You're signed in as a {current.role}.")
        st.stop()
    return current
