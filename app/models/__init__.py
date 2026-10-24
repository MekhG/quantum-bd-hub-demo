"""All tables. Importing this package registers every model on Base.metadata (Alembic relies on that)."""

from app.models.base import Base
from app.models.config_tables import (
    ActivityType,
    CadenceStep,
    LostReason,
    OptionList,
    Product,
    Stage,
    TaskAssignment,
    Track,
    cadence_step_activity_types,
)
from app.models.core import (
    ACADEMIC_ORG_TYPES,
    ORG_TYPES,
    Contact,
    Lead,
    Opportunity,
    OpportunityProduct,
    Organisation,
    User,
    lead_products,
)
from app.models.activities import Activity, ActivityParticipant
from app.models.compliance import AuditLog, ConsentLog, DataRequest, Suppression
from app.models.imports import ImportBatch, ImportBatchItem
from app.models.library import (
    Document,
    Objection,
    Script,
    activity_documents,
    document_products,
    document_tracks,
    objection_products,
    objection_tracks,
)
from app.models.community import CommunityMember, Event, EventParticipant
from app.models.content import ContentPost
from app.models.partners import DealRegistration, Installation, Partner
from app.models.diagrams import Diagram
from app.models.intelligence import NewsItem, NewsSource, OrgSignal, SignalType
from app.models.reports import ReportNote, WeeklyReport, WeeklyTarget
from app.models.procurement import (
    AppSetting,
    Bundle,
    BundleItem,
    PriceItem,
    ProcurementItem,
    ProcurementRule,
    Quote,
    QuoteLine,
)

__all__ = [
    "ContentPost",
    "DealRegistration",
    "Installation",
    "Partner",
    "CommunityMember",
    "Diagram",
    "Event",
    "EventParticipant",
    "NewsItem",
    "NewsSource",
    "OrgSignal",
    "ReportNote",
    "SignalType",
    "WeeklyReport",
    "WeeklyTarget",
    "ACADEMIC_ORG_TYPES",
    "AppSetting",
    "Bundle",
    "BundleItem",
    "Document",
    "Objection",
    "PriceItem",
    "ProcurementItem",
    "ProcurementRule",
    "Quote",
    "QuoteLine",
    "Script",
    "activity_documents",
    "document_products",
    "document_tracks",
    "objection_products",
    "objection_tracks",
    "ORG_TYPES",
    "Activity",
    "ActivityParticipant",
    "ActivityType",
    "AuditLog",
    "Base",
    "CadenceStep",
    "ConsentLog",
    "Contact",
    "DataRequest",
    "ImportBatch",
    "ImportBatchItem",
    "Lead",
    "LostReason",
    "Opportunity",
    "OpportunityProduct",
    "OptionList",
    "Organisation",
    "Product",
    "Stage",
    "Suppression",
    "TaskAssignment",
    "Track",
    "User",
    "cadence_step_activity_types",
    "lead_products",
]
