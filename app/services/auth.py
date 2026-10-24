"""Who is using the app, and what they may do.

On Streamlit Community Cloud the viewer list decides who can open the app at all;
this module then maps the signed-in email to a role (admin / member / viewer).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_dev_user_email, is_demo
from app.models import User

def auth_configured() -> bool:
    """True when the app has an [auth] section in its Secrets, i.e. sign-in with Google (or another OpenID
    provider) through st.login. Online this is required: since Streamlit 1.42, Community Cloud no longer tells
    the app who the viewer is, so the app signs people in itself."""
    try:
        import streamlit as st

        return "auth" in st.secrets
    except Exception:  # no secrets file at all (normal on a laptop)
        return False


def signed_in_email() -> str | None:
    """Online: the email the person signed in with (st.login). On a laptop without [auth]: DEV_USER_EMAIL from .env.

    DEV_USER_EMAIL must never be added to the cloud app's Secrets, or everyone would be treated as that person —
    so it is ignored whenever sign-in is configured. In the portfolio demo, the visitor picks a fictional persona.
    """
    if is_demo():
        try:
            from app.demo.personas import chosen_email

            return chosen_email()
        except Exception:  # outside Streamlit (scripts, tests)
            return get_dev_user_email()
    if auth_configured():
        try:
            import streamlit as st

            if st.user.is_logged_in:
                email = st.user.get("email")
                return str(email) if email else None
        except Exception:
            return None
        return None
    return get_dev_user_email()


def find_user(session: Session, email: str) -> User | None:
    return session.scalar(
        select(User).where(func.lower(User.email) == email.strip().lower(), User.is_active.is_(True))
    )


def can_edit(user: User) -> bool:
    return user.role in ("admin", "member")


def is_admin(user: User) -> bool:
    return user.role == "admin"
