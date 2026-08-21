"""outreach release: the human act at the end of the pipeline becomes a record

The report claims coverage "aus eigener Ansprache". That claim is only allowed to
rest on a letter somebody actually released — a draft nobody sent cannot have
caused a piece of press — so the ledger needs a place to record the release
before the metric layer can join against it.

Two columns, deliberately: a null ``released_at`` and an empty ``released_by``
are what make "still a draft" a fact rather than an inference. Existing rows are
drafts, which is exactly what they are.

Revision ID: 0018_outreach_release
Revises: 0017_client_facts
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_outreach_release"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outreach",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outreach",
        sa.Column("released_by", sa.String(length=80), nullable=False, server_default=""),
    )
    # The attribution join reads released letters by their release time, per
    # client, for one reporting period.
    op.create_index("ix_outreach_released_at", "outreach", ["released_at"])


def downgrade() -> None:
    op.drop_index("ix_outreach_released_at", table_name="outreach")
    op.drop_column("outreach", "released_by")
    op.drop_column("outreach", "released_at")
