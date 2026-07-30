"""angles: drafted positioning messages from the topic radar

The sweep gains a second kind of output. Until now everything stored was coverage
*of* a client; an angle is a message the consultant could send *to* a client,
drafted off market coverage that never mentions them. It is a new table rather
than columns on ``advisories`` because the two point in opposite directions — one
is an internal to-do list, the other is outward-facing text.

Revision ID: 0008_angles
Revises: 0007_client_website
Create Date: 2026-07-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_angles"
down_revision: str | None = "0007_client_website"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches models._EMPTY_JSON_ARRAY: existing rows and inserts that omit a list
# column get a valid empty array rather than NULL.
_EMPTY_JSON_ARRAY = "'[]'"


def upgrade() -> None:
    op.create_table(
        "angles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("credibility", sa.Text(), nullable=False, server_default=""),
        sa.Column("thesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("overclaim", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "statements",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(_EMPTY_JSON_ARRAY),
        ),
        sa.Column(
            "article_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(_EMPTY_JSON_ARRAY),
        ),
    )
    # Both indexes serve the Today column: it asks for one client's drafts, or one
    # local day's, on every render.
    op.create_index("ix_angles_client_id", "angles", ["client_id"])
    op.create_index("ix_angles_generated_at", "angles", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_angles_generated_at", table_name="angles")
    op.drop_index("ix_angles_client_id", table_name="angles")
    op.drop_table("angles")
