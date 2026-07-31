"""dismissed coverage: a human saying "this is not about my client"

The matcher is deliberately loose — it favours recall and lets Claude decide — but
a company named after a fictional planet, or one with a namesake in another
industry, collects articles that are simply not about it. Those sat in the archive
and in the counts with no way to remove them.

Dated rather than deleted, and the row stays: deleting the analysis would let the
next sweep re-match the same pair and re-analyse it, and the article would be back
by morning. The row is precisely what stops that.

Revision ID: 0011_dismiss_coverage
Revises: 0010_comms_guide
Create Date: 2026-07-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_dismiss_coverage"
down_revision: str | None = "0010_comms_guide"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analyses") as batch:
        batch.add_column(sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_analyses_dismissed_at", "analyses", ["dismissed_at"])


def downgrade() -> None:
    op.drop_index("ix_analyses_dismissed_at", table_name="analyses")
    with op.batch_alter_table("analyses") as batch:
        batch.drop_column("dismissed_at")
