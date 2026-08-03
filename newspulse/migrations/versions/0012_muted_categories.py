"""muted categories: the noise one mandate never wants to see

A listed retailer collects three near-identical share-price items a day — "die
Aktie zeigt Stabilität", "klettert deutlich", "bleibt stabil" — each scored 4-5
out of 10 and therefore filed beside a real event like a regulator's reprimand.
The category filter could hide them, but it forgot the choice on every page load,
so the same decision had to be made every morning. A consultant's verdict on that
was the sharpest thing in the review: it is the point at which the sixty-second
triage stops being sixty seconds, and the tab stops being opened.

Per client rather than global: "finanzen" is noise for a retailer whose ticker
runs daily and is the entire mandate for a bank.

Revision ID: 0012_muted_categories
Revises: 0011_dismiss_coverage
Create Date: 2026-08-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_muted_categories"
down_revision: str | None = "0011_dismiss_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(
            sa.Column(
                "muted_categories",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("muted_categories")
