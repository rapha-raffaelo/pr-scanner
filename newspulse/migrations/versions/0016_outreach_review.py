"""outreach review: a second model reads the letter before a human sends it

The model that wrote a pitch is the worst available judge of whether it oversells:
it chose every word for a reason it still believes. So a different provider reads
it and answers one narrow question — would this embarrass the sender — and the
answer is stored with the letter rather than shown once and lost.

Three columns rather than one, because "checked and clean" and "never checked"
must not look alike: ``reviewed_by`` is empty only in the second case.

Revision ID: 0016_outreach_review
Revises: 0015_outreach
Create Date: 2026-08-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_outreach_review"
down_revision: str | None = "0015_outreach"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outreach",
        sa.Column("review", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "outreach",
        sa.Column("reviewed_by", sa.String(length=80), nullable=False, server_default=""),
    )
    # Existing rows were never checked; the flag says "no objection on file",
    # which reads correctly next to an empty reviewed_by.
    op.add_column(
        "outreach",
        sa.Column(
            "review_ok", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
    )


def downgrade() -> None:
    op.drop_column("outreach", "review_ok")
    op.drop_column("outreach", "reviewed_by")
    op.drop_column("outreach", "review")
