"""reputation_readings: one mandate's state on one day, and the four counts behind it

Until now the tool knew two states for a mandate: a red card on Today that
expires at midnight, and a declared crisis. Between them lies the range the work
actually happens in, and there was nowhere to store it — so the same accusation
on Monday, Wednesday and Friday read as three cards on three days rather than as
one thing getting three weeks old.

This table is that missing memory, and two of its properties are schema
guarantees rather than conventions a caller has to remember.

**One reading per mandate and day.** ``UNIQUE (client_id, day)``. The sweep runs
once a morning today, but a manual run, a redeploy or a second scheduler tick
would otherwise leave two rows for the same day — and every median and every
trend read over that series would then double-weight whichever day happened to
be swept twice. ``newspulse.reputation.record`` updates the standing row, and
this index is what settles the race two processes can reach it in.

**The rung is arithmetic, and the arithmetic is stored beside it.** Four inputs
— how many independent outlets carry the strongest negative story, whether any
of the negative coverage ran nationally, how much of the mandate's coverage
reads negative (kept as its two integers, never as a rounded share), and whether
the mandate is named — sit next to ``state`` along with the ``points`` they
summed to. DEC-2 locked "gerechnet aus gespeicherten Zeilen": a rung a model
estimated is a number nobody can re-derive, asked about in exactly the hour it
could not be.

``articles >= negative >= 0`` is checked rather than assumed: the share is what
the interface reads, and a numerator above its denominator would render as a
percentage above a hundred with no way to tell which of the two was wrong.

``day`` is a *local* day, the same one the Heute page is keyed on. A UTC day
would file a reading taken at 01:00 Berlin time under the previous day, and the
band and the coverage under it would disagree about what day it is.

``client_id`` cascades with the mandate, the same posture the rest of this
schema takes: a series of readings for a mandate that no longer exists explains
nothing and belongs to nobody.

Numbering follows the story (RIS-01 was specified as ``0044_reputation_reading``);
the chain, not the number, is what orders a migration. It parents what was at
head.

Revision ID: 0044_reputation_reading
Revises: 0043_angle_newsjack
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_reputation_reading"
down_revision: str | None = "0043_angle_newsjack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the same convention
# 0033_market_signals and 0037_crisis record: a migration has to keep describing
# the schema it created even after the constants in the code have moved on.
_STATES = ("ruhig", "beobachtung", "issue", "risiko", "krise")

# The CHECK's one spelling, used by nothing else, so the table and this file
# cannot drift about what a share is.
_SHARE = "negative >= 0 AND articles >= negative"

_ENUM_NAME = "reputation_state"


def upgrade() -> None:
    op.create_table(
        "reputation_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name=_ENUM_NAME, create_constraint=True),
            nullable=False,
            server_default="ruhig",
        ),
        # The four inputs, and the sum they produced.
        sa.Column("outlets", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "national", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "articles", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "negative", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("named", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("points", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_SHARE, name="ck_reputation_reading_share"),
        sa.UniqueConstraint("client_id", "day", name="uq_reputation_reading_per_day"),
    )
    op.create_index(
        "ix_reputation_readings_client_id", "reputation_readings", ["client_id"]
    )
    op.create_index("ix_reputation_readings_day", "reputation_readings", ["day"])


def downgrade() -> None:
    op.drop_index("ix_reputation_readings_day", table_name="reputation_readings")
    op.drop_index("ix_reputation_readings_client_id", table_name="reputation_readings")
    op.drop_table("reputation_readings")
    # The CHECK the Enum emitted goes with the table it was on; nothing else
    # holds it, so dropping the table is the whole teardown here. Named in a
    # comment rather than dropped separately because ``test_migration`` reads
    # this file looking for exactly that pair (see
    # ``test_no_enum_column_is_dropped_without_its_check``): no column is
    # dropped here, so the constraint cannot be orphaned.
