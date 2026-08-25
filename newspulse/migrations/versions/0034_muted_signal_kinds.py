"""per-mandate mute for a market class

The category mute (0012) said "a listed retailer does not want its ticker in the
daily feed". This says the same thing one level out, about the three market
classes: a regulatory calendar is the entire job for a bank and pure noise for a
fashion label, and an event calendar is the reverse.

Same column shape as ``muted_categories`` on purpose — a JSON array of the
stored enum values, defaulting to "mute nothing" — so an existing row upgrades
into exactly the behaviour it had before this column existed.

Revision ID: 0034_muted_signal_kinds
Revises: 0033_market_signals
Create Date: 2026-08-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_muted_signal_kinds"
down_revision: str | None = "0033_market_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(
            sa.Column(
                "muted_signal_kinds",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("muted_signal_kinds")
