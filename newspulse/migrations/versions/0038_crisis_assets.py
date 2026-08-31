"""assets: the crisis as a second anchor for a generated text (UHR-02)

Three changes to ``assets``, and each is a schema guarantee rather than a
convention:

**A text can hang on a crisis.** ``crisis_id`` arrives as a nullable FK into
``crises``, CASCADE like the angle's: a crisis text whose crisis is gone cannot
be explained, and crises are only ever deleted with their whole mandate anyway.

**A text can hang on no angle — but never on nothing.** ``angle_id`` becomes
nullable, because a holding statement argues no position, and the new CHECK
(``angle_id IS NOT NULL OR crisis_id IS NOT NULL``) keeps a row from floating
free of both occasions.

**One unreleased draft per format per crisis.** The same partial UNIQUE the
angle-anchored rows already have, over ``(crisis_id, kind)`` where
``released_at IS NULL AND crisis_id IS NOT NULL`` — two workers writing the
same holding statement in the same minute is exactly the crisis-morning race
this exists for.

The batch rebuild (SQLite cannot ALTER a column in place) recreates the table
from reflection; SQLAlchemy 2.0 reflects the existing partial index predicate,
so ``ux_assets_angle_kind_unreleased`` survives with its WHERE clause intact.
The test suite verifies that rather than trusting it.

Numbering follows the story (UHR-02 was specified as ``0038_crisis_assets``);
the chain, not the number, is what orders a migration. It parents *both* heads:
``0037_crisis`` (UHR-01) and ``0041_angle_plan_hook`` (UHR-06) landed as
siblings off ``0036_visibility``, and this revision needs both anyway — the
``crises`` table its FK points into, and the ``assets`` state the plan chain
left behind — so it is the merge point that gives the chain one head again.

Revision ID: 0038_crisis_assets
Revises: 0041_angle_plan_hook, 0037_crisis
Create Date: 2026-08-31
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_crisis_assets"
down_revision: str | Sequence[str] | None = ("0041_angle_plan_hook", "0037_crisis")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One spelling for the predicate, used by CREATE and by nothing else, so the
# index and its documentation cannot drift.
_CRISIS_OPEN = "released_at IS NULL AND crisis_id IS NOT NULL"

_CRISIS_INDEX = "ux_assets_crisis_kind_unreleased"


def upgrade() -> None:
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("crisis_id", sa.Integer(), nullable=True))
        # Named, because batch mode refuses an anonymous constraint: the table
        # is rebuilt and every constraint has to be re-addressable.
        batch.create_foreign_key(
            "fk_assets_crisis_id_crises",
            "crises",
            ["crisis_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.alter_column("angle_id", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(
            "ck_assets_anchor", "angle_id IS NOT NULL OR crisis_id IS NOT NULL"
        )
    op.create_index("ix_assets_crisis_id", "assets", ["crisis_id"])
    # Both dialect spellings, like every partial index in this chain: the
    # predicate is a dialect keyword, and the one a backend does not recognise
    # is dropped rather than refused — which would leave a plain
    # UNIQUE(crisis_id, kind) forbidding a crisis a second released text.
    op.create_index(
        _CRISIS_INDEX,
        "assets",
        ["crisis_id", "kind"],
        unique=True,
        sqlite_where=sa.text(_CRISIS_OPEN),
        postgresql_where=sa.text(_CRISIS_OPEN),
    )


def downgrade() -> None:
    op.drop_index(_CRISIS_INDEX, table_name="assets")
    op.drop_index("ix_assets_crisis_id", table_name="assets")
    with op.batch_alter_table("assets") as batch:
        batch.drop_constraint("ck_assets_anchor", type_="check")
        # Refuses on a database that holds crisis texts, which is right: they
        # have no angle to fall back on, and silently deleting them would lose
        # released records.
        batch.alter_column("angle_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("crisis_id")
