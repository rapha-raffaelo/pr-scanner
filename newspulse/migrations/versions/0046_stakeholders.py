"""stakeholders, stakeholder_selections: the standing map, and the selection

The map hangs on the mandate, not on the issue: who the neighbours of a site
are and which association speaks for the industry does not change with the
occasion, and a map reinvented per incident is half wrong per incident. Two
tables carry it, and each holds one discipline at the schema so no future
writer has to remember it:

**stakeholders** — one group per row, one row per (client, group): the UNIQUE
is what makes "a proposal never overwrites a standing row" cheap to keep —
proposing the same group again cannot file a second copy. ``set_by`` says who
put the row there (the "modell" token, or a person), the same provenance the
profile keeps for every researched value.

**stakeholder_selections** — one group of the map, selected for one issue or
one crisis. Exactly one anchor (the XOR CHECK). ``reason`` is CHECKed
non-empty — a group selected without a stored sentence why the occasion
touches it is not stored, the same rule ``issue_signals.reason`` holds.
``position`` is 1-based and carries who set the order: the "modell" token
while it is the recommendation, a person's name once a person has sorted it,
which is the order that is kept.

Numbering follows the story (RIS-03 was specified as ``0046_stakeholders``);
the chain, not the number, is what orders a migration.

Revision ID: 0046_stakeholders
Revises: 0045_issues
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_stakeholders"
down_revision: str | None = "0045_issues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the convention every
# migration here keeps: a migration has to keep describing the schema it
# created even after the constants in the code moved on.
_LEVELS = ("hoch", "mittel", "niedrig")
_ENUM_NAME = "stakeholder_level"

# A selection hangs on exactly one occasion — an issue or a crisis.
_ONE_ANCHOR = "(issue_id IS NULL) <> (crisis_id IS NULL)"


def upgrade() -> None:
    op.create_table(
        "stakeholders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group_name", sa.Text(), nullable=False),
        sa.Column("betroffenheit", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "einfluss",
            sa.Enum(*_LEVELS, name=_ENUM_NAME, create_constraint=True),
            nullable=False,
            server_default="mittel",
        ),
        sa.Column("contact", sa.String(200), nullable=False, server_default=""),
        sa.Column("channel", sa.String(200), nullable=False, server_default=""),
        sa.Column("set_by", sa.String(80), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "group_name", name="uq_stakeholders_group"),
    )
    op.create_index("ix_stakeholders_client_id", "stakeholders", ["client_id"])

    op.create_table(
        "stakeholder_selections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "crisis_id",
            sa.Integer(),
            sa.ForeignKey("crises.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "stakeholder_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("info_need", sa.Text(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("position_set_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_ONE_ANCHOR, name="ck_stakeholder_selections_one_anchor"),
        sa.CheckConstraint("reason <> ''", name="ck_stakeholder_selections_reason"),
        sa.CheckConstraint("position >= 1", name="ck_stakeholder_selections_position"),
        sa.UniqueConstraint(
            "issue_id", "stakeholder_id", name="uq_stakeholder_selections_issue"
        ),
        sa.UniqueConstraint(
            "crisis_id", "stakeholder_id", name="uq_stakeholder_selections_crisis"
        ),
    )
    op.create_index(
        "ix_stakeholder_selections_issue_id", "stakeholder_selections", ["issue_id"]
    )
    op.create_index(
        "ix_stakeholder_selections_crisis_id", "stakeholder_selections", ["crisis_id"]
    )
    op.create_index(
        "ix_stakeholder_selections_stakeholder_id",
        "stakeholder_selections",
        ["stakeholder_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stakeholder_selections_stakeholder_id", table_name="stakeholder_selections"
    )
    op.drop_index(
        "ix_stakeholder_selections_crisis_id", table_name="stakeholder_selections"
    )
    op.drop_index(
        "ix_stakeholder_selections_issue_id", table_name="stakeholder_selections"
    )
    op.drop_table("stakeholder_selections")
    op.drop_index("ix_stakeholders_client_id", table_name="stakeholders")
    op.drop_table("stakeholders")
    # The CHECK the level Enum emitted goes with the ``stakeholders`` table it
    # was on; nothing else holds it, so dropping the tables is the whole
    # teardown.
