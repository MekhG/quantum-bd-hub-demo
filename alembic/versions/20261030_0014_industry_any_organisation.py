"""Any organisation, not only colleges: organisations get an industry, the type list becomes generic
(company, startup, school, research institute, government body, non-profit…), and old types are mapped across.

The industry list and the business contact types are configuration data: `python -m app.seed.seed_config` adds the
industry list to an existing database, and this migration adds the new contact types to the existing list.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORG_TYPES = ("college", "standalone", "university", "vendor", "state_org", "csr_company", "partner",
                 "individual", "other")
NEW_ORG_TYPES = ("company", "startup", "university", "college", "standalone", "school", "research_institute",
                 "government", "non_profit", "partner", "individual", "other")
# Old type → new type. Kept as a list so the downgrade can map back.
TYPE_MAP = {"vendor": "company", "state_org": "government", "csr_company": "company"}
# A sensible starting industry for organisations that already exist; the team can change any of them.
INDUSTRY_FOR_TYPE = {"college": "Higher education", "standalone": "Higher education",
                     "university": "Higher education", "state_org": "Government & public sector",
                     "csr_company": "Non-profit & CSR"}
NEW_CONTACT_TYPES = ("Founder/CXO", "Director/VP", "Manager", "Engineer/Scientist", "L&D/HR")


def upgrade() -> None:
    op.add_column("organisations", sa.Column("industry", sa.String(length=80), nullable=True))
    op.create_index(op.f("ix_organisations_industry"), "organisations", ["industry"], unique=False)

    orgs = sa.table("organisations", sa.column("org_type", sa.String), sa.column("industry", sa.String))
    bind = op.get_bind()
    # Industry first, while the old type still says where the organisation came from.
    for old, industry in INDUSTRY_FOR_TYPE.items():
        bind.execute(orgs.update().where(orgs.c.org_type == old).values(industry=industry))

    op.drop_constraint(op.f("ck_organisations_org_type_valid"), "organisations", type_="check")
    for old, new in TYPE_MAP.items():
        bind.execute(orgs.update().where(orgs.c.org_type == old).values(org_type=new))
    op.create_check_constraint(
        op.f("ck_organisations_org_type_valid"), "organisations", f"org_type IN {NEW_ORG_TYPES}"
    )
    op.alter_column("organisations", "org_type", server_default="company")

    options = sa.table(
        "option_lists", sa.column("list_name", sa.String), sa.column("value", sa.String),
        sa.column("aliases", sa.JSON), sa.column("sort_order", sa.Integer), sa.column("is_active", sa.Boolean),
    )
    present = set(bind.execute(sa.select(options.c.value).where(options.c.list_name == "contact_type")).scalars())
    if present:  # only top up a list that was seeded; a fresh database gets the full list from the seed
        start = len(present)
        for i, value in enumerate(v for v in NEW_CONTACT_TYPES if v not in present):
            bind.execute(options.insert().values(list_name="contact_type", value=value, aliases=[],
                                                 sort_order=start + i, is_active=True))


def downgrade() -> None:
    op.drop_constraint(op.f("ck_organisations_org_type_valid"), "organisations", type_="check")
    op.execute("UPDATE organisations SET org_type = 'vendor' WHERE org_type IN ('company', 'startup')")
    op.execute("UPDATE organisations SET org_type = 'state_org' WHERE org_type = 'government'")
    op.execute("UPDATE organisations SET org_type = 'other' "
               "WHERE org_type IN ('school', 'research_institute', 'non_profit')")
    op.create_check_constraint(
        op.f("ck_organisations_org_type_valid"), "organisations", f"org_type IN {OLD_ORG_TYPES}"
    )
    op.alter_column("organisations", "org_type", server_default="college")
    op.drop_index(op.f("ix_organisations_industry"), table_name="organisations")
    op.drop_column("organisations", "industry")
