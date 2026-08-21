"""reports: the released document frozen, and why a finding was dropped

``reports.snapshot`` is one nullable JSON column. A draft's evidence is ids
resolved against the archive every time it renders, which is what lets a claim
notice that its ground moved. A released report may not do that: it is the
artefact a client was sent, and a piece of coverage dismissed in October must not
change what the September document says. So the release copies the document into
this column and every later render reads the copy.

Nullable rather than defaulted to ``{}``: null means "never released", which is
the same fact ``released_at`` already carries, and an empty object would be a
frozen document that says nothing.

``report_findings.drop_reason`` is the other half of the review surface's rule
that a dropped finding stays visible rather than disappearing. Visible without a
reason invites the same claim to be argued down again next month.

Revision ID: 0029_report_snapshot
Revises: 0028_reports
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_report_snapshot"
down_revision: str | None = "0028_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "report_findings",
        sa.Column("drop_reason", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("report_findings", "drop_reason")
    op.drop_column("reports", "snapshot")
