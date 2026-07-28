"""per-client competitor sets

``clients.is_competitor`` (0004) says a company is monitored for benchmarking
rather than reported to. It cannot say *whose* benchmark it is — and that is the
question share of voice actually asks. Zalando competes with About You and Otto;
Siemens with ABB. A single portfolio-wide flag collapses those into one pool, and
a share computed across unrelated industries is meaningless.

This adds the missing relation: many-to-many between clients, because one
company can be a benchmark for several mandates. Both directions are stored
explicitly rather than inferred, so benchmarking a mandate against a market
leader does not silently add the mandate to the leader's own comparison set.

``is_competitor`` stays. The two answer different questions: this table is "who
is X measured against", the flag is "is this a paying mandate or a company we
only watch" — which is what keeps a benchmark out of the morning digest.

Revision ID: 0005_client_competitors
Revises: 0004_workflow_and_advisories
Create Date: 2026-07-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_client_competitors"
down_revision: str | None = "0004_workflow_and_advisories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_competitors",
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "competitor_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # A company is not its own competitor. Enforced in the schema so no
        # caller has to remember it and a raw INSERT cannot create the self-link
        # that would double-count a client in its own share.
        sa.CheckConstraint(
            "client_id != competitor_id", name="ck_client_competitors_distinct"
        ),
    )


def downgrade() -> None:
    op.drop_table("client_competitors")
