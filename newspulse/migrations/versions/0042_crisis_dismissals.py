"""crisis_dismissals: the proposal a person stood down without declaring (UHR-03)

One table, one guarantee. A dismissal records that a person looked at a crisis
offer and pressed "Verwerfen" — the opposite statement from a ``crises`` row,
which records that a person declared one. Kept apart on purpose: written as an
instantly-closed crisis, the mandate's record would carry a phantom crisis, the
Krise tab would appear for a mandate that never had one, and every reader of
``crises`` would need a secret marker to tell the two apart.

**One dismissal per (mandate, trigger).** A plain UNIQUE over the pair, so a
double click, a second browser tab and a replayed POST cannot grow copies —
``newspulse.crisis.dismiss`` hands the standing row back rather than letting the
caller meet the IntegrityError, the same posture ``declare`` takes.

Both FKs CASCADE, the same posture the ``crises`` triggers take: a dismissal of
coverage that no longer exists silences nothing and explains nothing, so it does
not outlive it.

Revision ID: 0042_crisis_dismissals
Revises: 0038_crisis_assets
Create Date: 2026-08-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_crisis_dismissals"
down_revision: str | None = "0038_crisis_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the same convention the
# whole chain records: a migration keeps describing the schema it created even
# after the constant in the code has moved on. 80 is CRISIS_DECLARED_BY_MAX.
_BY_MAX = 80


def upgrade() -> None:
    op.create_table(
        "crisis_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dismissed_by", sa.String(_BY_MAX), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "client_id", "article_id", name="uq_crisis_dismissals_once"
        ),
    )
    op.create_index(
        "ix_crisis_dismissals_client_id", "crisis_dismissals", ["client_id"]
    )
    op.create_index(
        "ix_crisis_dismissals_article_id", "crisis_dismissals", ["article_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_crisis_dismissals_article_id", table_name="crisis_dismissals")
    op.drop_index("ix_crisis_dismissals_client_id", table_name="crisis_dismissals")
    op.drop_table("crisis_dismissals")
