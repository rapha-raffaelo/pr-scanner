"""plan_hooks: the editorial plan's entries, each resolving to a stored row

One table, and the shape of its date is the feature's rule made schema: ``month``
is a ``"YYYY-MM"`` string and ``day`` is nullable, because a source that only
names a month yields a hook that only names a month — the missing day is never
guessed, and there is no column arrangement under which it could be.

``source_kind``/``source_id`` are the evidence: the id of a row in the table the
kind names (``market_signals``, ``topic_hits``, ``analyses``). Deliberately not a
foreign key — it points into one of three tables depending on the kind, and the
resolution guard lives in :func:`newspulse.plan._resolves`, which refuses to
store a hook whose row does not exist. The UNIQUE over (client, kind, source) is
what keeps a recompute from filing a fresh proposal next to the "verworfen" a
person already recorded against the same row.

Both enums ship with their CHECKs, the same posture every other enum column in
this schema takes: a raw INSERT cannot store a fourth source class, because a
fourth class is exactly the door DEC-4 closed.

Revision ID: 0040_plan_hooks
Revises: 0036_visibility
Create Date: 2026-08-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_plan_hooks"
down_revision: str | None = "0036_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the convention
# 0033_market_signals records: a migration keeps describing the schema it
# created even after the enum in the code has moved on.
_SOURCES = ("marktsignal", "thema", "vorjahr")
_STATES = ("vorgeschlagen", "angenommen", "verworfen")


def upgrade() -> None:
    op.create_table(
        "plan_hooks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_kind",
            sa.Enum(*_SOURCES, name="hook_source", create_constraint=True),
            nullable=False,
        ),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("day", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("format", sa.String(length=40), nullable=False, server_default=""),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name="hook_state", create_constraint=True),
            nullable=False,
            server_default="vorgeschlagen",
        ),
        # A move is a touch: a moved hook survives recomputes even while its
        # state is still the machine's.
        sa.Column("moved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "client_id", "source_kind", "source_id", name="uq_plan_hooks_source"
        ),
        sa.CheckConstraint(
            "day IS NULL OR (day >= 1 AND day <= 31)", name="ck_plan_hooks_day"
        ),
    )
    op.create_index("ix_plan_hooks_client_id", "plan_hooks", ["client_id"])
    # Every read of the plan asks for one mandate's months in the window.
    op.create_index("ix_plan_hooks_client_month", "plan_hooks", ["client_id", "month"])


def downgrade() -> None:
    op.drop_index("ix_plan_hooks_client_month", table_name="plan_hooks")
    op.drop_index("ix_plan_hooks_client_id", table_name="plan_hooks")
    op.drop_table("plan_hooks")
