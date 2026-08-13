"""outreach: the impulse, written at one recipient

The Impulse page carried two panels whose difference nobody could state — a
positioning draft from the market, and a "recommendation" from the mandate's own
press. Both ended the same way: a text sent to a journalist. So the second panel
is gone and the recommendation became a button on the first, and this table holds
what that button produces.

One row per (impulse, recipient): the same position reads differently to the
reporter who covered the story it answers than to a trade title that never has.

Revision ID: 0015_outreach
Revises: 0014_impulse_note
Create Date: 2026-08-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_outreach"
down_revision: str | None = "0014_impulse_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outreach",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "angle_id",
            sa.Integer(),
            sa.ForeignKey("angles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("journalist", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("outlet", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_outreach_angle_id", "outreach", ["angle_id"])
    op.create_index("ix_outreach_client_id", "outreach", ["client_id"])
    op.create_index("ix_outreach_generated_at", "outreach", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_outreach_generated_at", table_name="outreach")
    op.drop_index("ix_outreach_client_id", table_name="outreach")
    op.drop_index("ix_outreach_angle_id", table_name="outreach")
    op.drop_table("outreach")
