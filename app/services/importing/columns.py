"""Which spreadsheet columns each import understands, and how header text is matched to them.

Headers are matched ignoring case, spacing, punctuation and anything in brackets,
so 'Type (College/ Standalone/University)' is recognised as 'Type' and
'Website (to fill)' as 'Website'.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.importing.normalise import norm_key, strip_parentheses


@dataclass(frozen=True)
class Field:
    key: str
    label: str  # the column name shown to people (matches the team's sheets)
    aliases: tuple[str, ...]
    required: bool = False


# ---- shared building blocks ----

_INSTITUTION = Field("institution", "Institution Name",
                     ("institution name", "institution", "institute name", "college name", "organisation",
                      "organization", "academia", "university", "institution university",
                      "institution organisation", "institution organization"), required=True)
_CONTACT_NAME = Field("contact_name", "Contact Name",
                      ("contact name", "contact person", "name of contact", "contact", "recipient name", "poc",
                       "poc name", "faculty name", "professor hod", "hod prof", "hod professor",
                       # A bare 'Name' next to an 'Institution Name' column is the person.
                       "name", "full name"))
_EMAIL = Field("email", "Email Id",
               ("email id", "email", "email address", "e-mail", "e mail", "mail id", "mail"))
_PHONE = Field("phone", "Contact Number",
               ("contact number", "phone number", "phone", "mobile", "mobile number", "contact no", "phone no",
                "mobile no", "contacts"))
_LINKEDIN = Field("linkedin", "LinkedIn", ("linkedin", "linkedin url", "linkedin profile"))
_CONTACT_TYPE = Field("contact_type", "Contact Type", ("contact type", "type of contact", "role"))
_DESIGNATION = Field("designation", "Designation", ("designation", "position", "title"))
_DEPARTMENT = Field("department", "Department", ("department", "dept", "department name"))
_OWNER = Field("owner", "Owner",
               ("owner", "bd person", "bd owner", "poc", "assigned to", "handled by"))
_CHANNEL = Field("channel", "Channel",
                 ("channel", "lead channel", "lead via", "lead type", "lead from", "lead by"))
_STATUS = Field("status", "Status", ("status", "stage", "lead status"))
_TIER = Field("tier", "Tier", ("tier", "institution tier"))
_POTENTIAL = Field("potential", "Potential", ("potential", "probability", "chance"))
_AISHE = Field("aishe", "AISHE Code", ("aishe code", "aishe", "aishe id"))
_WEBSITE = Field("website", "Website", ("website", "web site", "url", "website link", "web", "seed url"))
_LOCATION = Field("location", "Location", ("location", "place", "area"))
_DISTRICT = Field("district", "District", ("district", "city"))
_STATE = Field("state", "State", ("state", "state name"))
_ORG_TYPE = Field("org_type", "Type", ("type", "institution type", "type of institution"))
_NOTES = Field("notes", "Notes", ("notes", "note", "remarks", "comments", "comment"))
_PRODUCT_FIT = Field("product_fit", "Product Fit",
                     ("product fit", "products of interest", "product interest", "products enquired for",
                      "products enquired", "likely product fit", "product shown interest in",
                      "product interested in", "interested in"))
_PRODUCT_FIT_2 = Field("product_fit2", "Product Fit 2", ("product fit 2", "product fit2", "product fit ii"))

TRACKER_FIELDS: tuple[Field, ...] = (
    _OWNER,
    _STATUS,
    _TIER,
    _POTENTIAL,
    _LOCATION,
    _ORG_TYPE,
    _INSTITUTION,
    _CONTACT_NAME,
    _EMAIL,
    _PHONE,
    _CONTACT_TYPE,
    _DEPARTMENT,
    Field("research", "Faculty Research Interests",
          ("faculty research interests", "research interests", "research interest", "research area")),
    _PRODUCT_FIT,
    _PRODUCT_FIT_2,
    _CHANNEL,
    _DESIGNATION,
    _LINKEDIN,
    _AISHE,
    _WEBSITE,
    _STATE,
    _DISTRICT,
    Field("nqm", "NQM", ("nqm", "nqm applied", "nqm flag", "nqm lead")),
    Field("funding", "Funding Source", ("funding source grant", "funding source", "funding", "grant")),
    Field("segment", "Segment", ("segment",)),
    Field("why", "Why it qualifies", ("why it qualifies", "why")),
    Field("dept_to_scrape", "Department to scrape", ("department to scrape",)),
    _NOTES,
)

RESPONSES_FIELDS: tuple[Field, ...] = (
    _INSTITUTION,
    _CONTACT_NAME,
    _EMAIL,
    _PHONE,
    _CONTACT_TYPE,
    Field("response", "Responses",
          ("responses", "response", "response text", "message", "query", "enquiry", "reply"), required=True),
    _OWNER,
    Field("date", "Date", ("date", "response date", "received on", "date received", "received date"),
          required=True),
    Field("source", "Source",
          ("source", "response source", "response type", "mode", "via", "enquiry source", "origin"),
          required=True),
    _CHANNEL,
    _DEPARTMENT,
    _DESIGNATION,
    _AISHE,
    _WEBSITE,
    _LOCATION,
    _STATE,
)

CLIENTS_FIELDS: tuple[Field, ...] = (
    _INSTITUTION,
    _CONTACT_NAME,
    _EMAIL,
    _CONTACT_TYPE,
    Field("products", "Products", ("products", "product", "products purchased", "product purchased")),
    Field("date", "Date", ("date", "po date", "order date", "po released date", "purchase date", "po released on")),
    _CHANNEL,
    _PHONE,
    _DEPARTMENT,
    _OWNER,
    Field("value", "Value (INR)", ("value", "amount", "po value", "deal value", "order value", "value inr")),
    _AISHE,
    _WEBSITE,
    _LOCATION,
    _STATE,
    _ORG_TYPE,
)

COLLEGES_FIELDS: tuple[Field, ...] = (
    _ORG_TYPE,
    _AISHE,
    Field("institution", "Name", ("name", "institution name", "college name", "institute name"), required=True),
    _WEBSITE,
    _STATE,
    _DISTRICT,
    _LOCATION,
    Field("university", "University Name",
          ("university name", "affiliating university", "affiliated university", "affiliated to")),
    Field("category", "College / Standalone Type",
          ("college type", "standalone type", "institution category", "sub type")),
    Field("management", "Management",
          ("management", "manegement", "management type", "university type management", "ownership")),
)

MINI_FIELDS: tuple[Field, ...] = (
    Field("date", "Date", ("date", "enquiry date", "date of enquiry")),
    Field("contact_name", "Name", ("name", "full name", "contact name"), required=True),
    _EMAIL,
    Field("phone", "Contact number", _PHONE.aliases),
    Field("product_fit", "Product shown interest in", (*_PRODUCT_FIT.aliases, "product", "products")),
    _STATUS,
    _POTENTIAL,
    Field("about", "About", ("about", "about them", "background", "profile")),
    _NOTES,
    Field("institution", "Institution / Organisation",
          ("institution organisation", "institution organization", "institution", "organisation",
           "organization", "company", "college", "university", "institution name")),
    _OWNER,
    _CHANNEL,
    _STATE,
)

FIELDS_BY_KIND: dict[str, tuple[Field, ...]] = {
    "tracker": TRACKER_FIELDS,
    "responses": RESPONSES_FIELDS,
    "clients": CLIENTS_FIELDS,
    "colleges": COLLEGES_FIELDS,
    "mini": MINI_FIELDS,
}


def header_key(header: str) -> str:
    return norm_key(strip_parentheses(str(header)).replace("_", " "))


@dataclass
class ColumnMapping:
    columns: dict[str, str]  # field key → header text in the sheet
    missing_required: list[Field]
    missing_optional: list[Field]
    unused_headers: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing_required


def map_columns(headers: list[str], kind: str) -> ColumnMapping:
    fields = FIELDS_BY_KIND[kind]
    keyed: dict[str, str] = {}
    for h in headers:
        if str(h).strip():
            keyed.setdefault(header_key(h), h)
    columns: dict[str, str] = {}
    taken: set[str] = set()
    for field in fields:
        for alias in field.aliases:
            key = norm_key(alias)
            if key in keyed and key not in taken:
                columns[field.key] = keyed[key]
                taken.add(key)
                break
    missing_required = [f for f in fields if f.required and f.key not in columns]
    missing_optional = [f for f in fields if not f.required and f.key not in columns]
    unused = [h for k, h in keyed.items() if k not in taken]
    return ColumnMapping(columns, missing_required, missing_optional, unused)


def header_score(cells: list[str]) -> int:
    """How many cells look like a known column name — used to find the header row."""
    known = {norm_key(a) for fields in FIELDS_BY_KIND.values() for f in fields for a in f.aliases}
    return sum(1 for c in cells if header_key(c) in known)
