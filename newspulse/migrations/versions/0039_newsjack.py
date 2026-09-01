"""newsjack_opportunities: one market story weighed for one mandate (UHR-04)

One table for both outcomes of the standing check. A ``belegt`` row is the
opportunity the fast lane surfaces; a ``duenn`` or ``keins`` row is the
rejection, stored with its reason — and either kind is what stops the next
scan from spending a second model call on the same story.

Two properties are schema guarantees rather than conventions:

* **One row per story per mandate.** ``article_id`` is the story's *origin* —
  its earliest piece — and the UNIQUE over (client_id, article_id) means a
  second scan, which re-clusters the same stored rows to the same origin,
  cannot file a second row even if its pre-check raced another process.
* **The verdict is one of exactly three answers.** The enum ships with its
  CHECK, the posture every other enum column in this schema takes: a raw
  INSERT cannot store a fourth standing, because the fourth answer is exactly
  what the feature refuses to have.

``window_ends_at`` is NOT NULL and fixed at creation (origin ``published_at``
plus the configured hours), so an opportunity expires by comparison against
the clock — whether or not a run ever happens again — and keeps the window it
was created under if the configuration later changes.

Numbering follows the story (UHR-04 was specified as ``0039_newsjack``); the
chain, not the number, is what orders a migration, and this parents the
current single head ``0038_crisis_assets``.

Revision ID: 0039_newsjack
Revises: 0038_crisis_assets
Create Date: 2026-08-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_newsjack"
down_revision: str | None = "0038_crisis_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the convention
# 0033_market_signals records: a migration keeps describing the schema it
# created even after the enum in the code has moved on.
_STANDINGS = ("belegt", "duenn", "keins")


def upgrade() -> None:
    op.create_table(
        "newsjack_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The story's origin — its earliest piece, never merely the one a scan
        # happened to see first. CASCADE like the crisis trigger's.
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "standing",
            sa.Enum(*_STANDINGS, name="newsjack_standing", create_constraint=True),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("pickup_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # The fast lane's stand-down (UHR-05): stamped, never deleted, because
        # the row is what keeps the story from coming back.
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        # The standards the standing check was composed under — same terms as
        # angles.brain_version. NULL is spoken for ("stored before there was
        # anything to stamp"), so no server default.
        sa.Column("brain_version", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "client_id", "article_id", name="uq_newsjack_client_article"
        ),
    )
    op.create_index(
        "ix_newsjack_opportunities_client_id", "newsjack_opportunities", ["client_id"]
    )
    op.create_index(
        "ix_newsjack_opportunities_article_id",
        "newsjack_opportunities",
        ["article_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_newsjack_opportunities_article_id", table_name="newsjack_opportunities"
    )
    op.drop_index(
        "ix_newsjack_opportunities_client_id", table_name="newsjack_opportunities"
    )
    op.drop_table("newsjack_opportunities")
