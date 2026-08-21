"""profile_proposals: what the refresh found, waiting for a yes

The background profile refresh proposes and never writes, so its findings need
somewhere to live between the 06:10 sweep that produced them and the person who
decides on them at nine. That used to be a dict in the web process — fine while
only a button wrote to it, and not fine now: a deploy or a restart dropped
everything the sweep had found, silently.

Also adds ``clients.profile_checked_at`` and ``clients.profile_note``, following
``impulse_checked_at``/``impulse_note``: a profile checked this morning and one
that has aged for a year must not look alike, and neither must a check that found
nothing and a check that broke.

Revision ID: 0025_profile_proposals
Revises: 0024_assets
Create Date: 2026-08-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_profile_proposals"
down_revision: str | None = "0024_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_title", sa.Text(), nullable=False, server_default=""),
        sa.Column("previous_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_by", sa.String(length=80), nullable=False, server_default=""),
        # One proposal per field per mandate: a second refresh replaces this
        # client's outstanding set rather than stacking a fresh guess beside it.
        sa.UniqueConstraint("client_id", "key", name="uq_profile_proposals_key"),
    )
    op.create_index("ix_profile_proposals_client_id", "profile_proposals", ["client_id"])
    with op.batch_alter_table("clients") as batch:
        batch.add_column(
            sa.Column("profile_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "profile_note", sa.Text(), nullable=False, server_default=""
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("profile_note")
        batch.drop_column("profile_checked_at")
    op.drop_index("ix_profile_proposals_client_id", table_name="profile_proposals")
    op.drop_table("profile_proposals")
