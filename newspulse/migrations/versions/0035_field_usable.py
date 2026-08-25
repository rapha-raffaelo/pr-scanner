"""whether the industry term is one the press actually writes

The market page has to explain a missing search half without accusing a term
nobody measured. The measurement costs a live Google News search per term, so it
is produced by the sweep and stored here rather than asked while a page renders
— the same reasoning that put ``profile_checked_at`` on this table.

Nullable on purpose, and not backfilled. NULL means "not established": either
the sweep has not reached this mandate yet or its last probe could not reach the
search at all. A backfill of 0 would read as "the press does not write this
word" for every existing row, which is exactly the false accusation the column
exists to prevent.

Revision ID: 0035_field_usable
Revises: 0034_muted_signal_kinds
Create Date: 2026-08-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_field_usable"
down_revision: str | None = "0034_muted_signal_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("field_usable", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column("field_checked_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("field_checked_at")
        batch.drop_column("field_usable")
