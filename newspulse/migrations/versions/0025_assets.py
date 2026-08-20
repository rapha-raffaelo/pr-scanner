"""assets: every other format an agency delivers, in one table

One table for six formats, and deliberately not six more columns on ``outreach``.
A letter's ``journalist`` and ``outlet`` are its recipient; five of the six
formats here have no recipient at all. Widening that table would leave most rows
with most columns empty and every query guessing which shape it was reading. The
letter keeps its table and its ledger; the two are read together where both
belong.

``kind`` is a closed enum with a CHECK behind it, like ``analyses.category``: the
kind selects the format definition that says what the text must contain, so a row
carrying a kind nothing defines would be a text nothing can validate.

Six review columns rather than two, for the reason ``0016_outreach_review`` gave
and one more. "Checked and clean" and "never checked" must not look alike, which
is why ``reviewed_by`` exists. And a breach of the client's own written guide is
not the same kind of finding as a tone remark, so it gets its own three columns
rather than being averaged into the first verdict.

The revision number skips ahead of the current head: numbers are handed out per
feature so two features in flight cannot collide, and the chain, not the number,
is what orders a migration.

Revision ID: 0025_assets
Revises: 0017_client_facts
Create Date: 2026-08-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_assets"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept literal rather than imported from ``newspulse.models``: a migration
#: describes the schema at one moment in time, and adding a seventh format later
#: must not silently rewrite what this revision did.
_KINDS = (
    "pressemitteilung",
    "statement",
    "qa",
    "talking_points",
    "gastbeitrag",
    "interview_briefing",
)


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "angle_id",
            sa.Integer(),
            sa.ForeignKey("angles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_KINDS, name="assetkind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("speaker", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "reviewed_by", sa.String(length=80), nullable=False, server_default=""
        ),
        sa.Column(
            "review_ok", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("guide_review", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "guide_reviewed_by", sa.String(length=80), nullable=False, server_default=""
        ),
        sa.Column(
            "guide_review_ok", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "released_by", sa.String(length=80), nullable=False, server_default=""
        ),
    )
    op.create_index("ix_assets_client_id", "assets", ["client_id"])
    op.create_index("ix_assets_angle_id", "assets", ["angle_id"])
    op.create_index("ix_assets_generated_at", "assets", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_assets_generated_at", table_name="assets")
    op.drop_index("ix_assets_angle_id", table_name="assets")
    op.drop_index("ix_assets_client_id", table_name="assets")
    op.drop_table("assets")
