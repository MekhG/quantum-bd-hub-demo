"""Numbers behind the Home dashboard. A handful of grouped queries, so the page stays quick over the internet."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Activity, Contact, Lead, Organisation, Stage, Track


@dataclass
class Dashboard:
    institutions: int
    contacts: int
    open_leads: int
    clients: int
    nqm_leads: int
    unverified_contacts: int
    leads_without_activity: int
    stages: pd.DataFrame  # Stage, outcome, sort_order, Leads (every stage, zeros included)
    tracks: pd.DataFrame  # Track, Leads, Clients, NQM


def _count(session: Session, query) -> int:
    return int(session.scalar(query) or 0)


def load_dashboard(session: Session) -> Dashboard:
    stage_rows = session.execute(
        select(Stage.name, Stage.outcome, Stage.sort_order, func.count(Lead.id))
        .outerjoin(Lead, Lead.stage_id == Stage.id)
        .where(Stage.is_active.is_(True))
        .group_by(Stage.id)
        .order_by(Stage.sort_order)
    ).all()
    stages = pd.DataFrame(stage_rows, columns=["Stage", "outcome", "sort_order", "Leads"])

    won = case((Stage.outcome == "won", 1), else_=0)
    nqm = case((Lead.nqm_flag.is_(True), 1), else_=0)
    track_rows = session.execute(
        select(Track.name, func.count(Lead.id), func.coalesce(func.sum(won), 0), func.coalesce(func.sum(nqm), 0))
        .outerjoin(Lead, Lead.track_id == Track.id)
        .outerjoin(Stage, Stage.id == Lead.stage_id)
        .where(Track.is_active.is_(True))
        .group_by(Track.id)
        .order_by(Track.sort_order)
    ).all()
    tracks = pd.DataFrame(track_rows, columns=["Track", "Leads", "Clients", "NQM"])

    open_outcomes = ("open", "nurture")
    has_activity = select(Activity.id).where(Activity.lead_id == Lead.id).exists()
    return Dashboard(
        institutions=_count(session, select(func.count(Organisation.id)).where(Organisation.org_type != "individual")),
        contacts=_count(session, select(func.count(Contact.id))),
        open_leads=int(stages.loc[stages["outcome"].isin(open_outcomes), "Leads"].sum()) if not stages.empty else 0,
        clients=int(stages.loc[stages["outcome"] == "won", "Leads"].sum()) if not stages.empty else 0,
        nqm_leads=_count(session, select(func.count(Lead.id)).where(Lead.nqm_flag.is_(True))),
        unverified_contacts=_count(
            session,
            select(func.count(Contact.id)).where(Contact.verified.is_(False), Contact.do_not_contact.is_(False),
                                                 Contact.email.is_not(None)),
        ),
        leads_without_activity=_count(
            session,
            select(func.count(Lead.id)).join(Stage, Stage.id == Lead.stage_id)
            .where(Stage.outcome.in_(open_outcomes), ~has_activity),
        ),
        stages=stages,
        tracks=tracks,
    )
