"""add is_relevant to analyses

The analyzer (NP-05) produces a boolean ``is_relevant`` per (article, client) —
its primary relevance judgment — but the initial schema (0001) had no column for
it, so NP-06 would have nowhere to persist it. Add the column here as a bridge
migration, NOT NULL with a DB-level DEFAULT 0 so existing rows and raw INSERTs
that omit it are valid.

Revision ID: 0002_add_is_relevant
Revises: 0001_initial
Create Date: 2026-07-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_is_relevant"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table so SQLite (no native ADD COLUMN for every case / no
    # DROP COLUMN) is handled by table-recreate under the hood.
    with op.batch_alter_table("analyses") as batch:
        batch.add_column(
            sa.Column(
                "is_relevant",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("analyses") as batch:
        batch.drop_column("is_relevant")
