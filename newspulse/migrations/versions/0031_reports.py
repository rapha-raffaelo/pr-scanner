"""reports: the client report as an object, and the findings under it

Two tables. ``reports`` is one row per (client, period), which is why the UNIQUE
is here rather than in the code that writes it: generating July twice is one
report drafted twice, and two rows would put two documents with the same date and
different sentences in front of the same client.

``report_findings`` carries ``evidence_ids`` as JSON pointing at ``analyses``.
Deliberately not a link table with a foreign key: the ids are a *citation*, and a
citation has to survive the row it points at going away. A cascading FK would
silently shrink a released document when an article was later dismissed, where
the point of storing the id is that the report can notice its ground moved and
say so.

This is the revision id the report feature reserves. It is taken here rather than
renumbered down to the next free slot: the ids in between belong to features
already building against them, and a revision that changed its name after the
fact would leave every database that ran it unable to find its own head.

The obligation that comes with the gap: whoever merges a sibling revision that
also chains from ``0018_outreach_ledger`` has to re-point one of the two, so the
tree keeps a single head. Nothing here can detect that on its own — it appears at
merge, not in this branch — so ``test_the_migration_chain_has_exactly_one_head``
asserts it, and the suite goes red instead of the deploy.

Revision ID: 0031_reports
Revises: 0030_asset_brain_version
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_reports"
down_revision: str | None = "0030_asset_brain_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The states a report can be in, as ``models.ReportState`` spells them. Repeated
#: here rather than imported, like every other revision in this directory: a
#: migration describes the schema at its own point in history and must not change
#: when the enum later does.
_STATES = ("entwurf", "freigegeben")

#: The finding kinds, as ``models.ReportFindingKind`` spells them, for the same
#: reason.
_KINDS = ("sichtbarkeit", "risiko", "wirkung", "botschaft")

#: DB-level DEFAULT for the JSON array column, matching ``models._EMPTY_JSON_ARRAY``:
#: a raw INSERT that omits the evidence must not be able to violate NOT NULL.
_EMPTY_JSON_ARRAY = "[]"


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name="report_state", create_constraint=True),
            nullable=False,
            server_default="entwurf",
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id", "period_start", "period_end", name="uq_reports_client_period"
        ),
    )
    op.create_index("ix_reports_client_id", "reports", ["client_id"])

    op.create_table(
        "report_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_KINDS, name="report_finding_kind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("consequence", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "evidence_ids", sa.JSON(), nullable=False, server_default=_EMPTY_JSON_ARRAY
        ),
        sa.Column("kept", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_findings_report_id", "report_findings", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_report_findings_report_id", table_name="report_findings")
    op.drop_table("report_findings")
    op.drop_index("ix_reports_client_id", table_name="reports")
    op.drop_table("reports")
