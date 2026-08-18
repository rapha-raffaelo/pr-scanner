"""client_facts: the deep-dive profile, and where each line of it came from

Fifteen columns on ``clients`` would have been simpler and wrong. A fact a machine
read on the internet and a fact the consultant knows from the last kick-off must
not look alike on the page, so every row carries its source and its author.

Revision ID: 0017_client_facts
Revises: 0016_outreach_review
Create Date: 2026-08-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_client_facts"
down_revision: str | None = "0016_outreach_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_facts",
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
        sa.Column("filled_by", sa.String(length=80), nullable=False, server_default="mensch"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "key", name="uq_client_facts_key"),
    )
    op.create_index("ix_client_facts_client_id", "client_facts", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_client_facts_client_id", table_name="client_facts")
    op.drop_table("client_facts")
