"""scenarios, their stakeholders, their triggers, and the response options

Four tables carry RIS-04, and each holds one discipline at the schema so no
future writer has to remember it:

**scenarios** — three courses per issue, one row per kind (the UNIQUE says so).
``likelihood`` is an Enum over four *words*: a percentage out of a model claims
an accuracy that does not exist, and it is that number which gets quoted back
four weeks later, so the column cannot hold one. ``narrative`` is CHECKed
non-empty.

**scenario_stakeholders** — the affected groups, as pointers into the standing
map rather than as rows of their own, exactly as ``stakeholder_selections``
does.

**scenario_triggers** — DEC-5's closed set of machine-checkable conditions.
``fired_at`` is the latch, and it is a *column* because the acceptance says a
fired trigger must not fire again "auch nicht nach einem Neustart"; anything
held in the process would re-announce every standing trigger on the next boot.
The CHECK ties a firing to the line saying what matched.

**response_options** — at least three per issue, one of them always "nicht
reagieren" (``no_response``), each with Nutzen, Risiko and Eskalationspotenzial.
The CHECK holds that the recommended option names a speed, so "schnell" and
"sofort" cannot come to mean the same thing.

Numbering follows the story (RIS-04 was specified as ``0047_scenarios``); the
chain, not the number, is what orders a migration.

Revision ID: 0047_scenarios
Revises: 0046_stakeholders
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_scenarios"
down_revision: str | None = "0046_stakeholders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the convention every
# migration here keeps: a migration has to keep describing the schema it
# created even after the constants in the code moved on.
_KINDS = ("bester", "wahrscheinlicher", "schlechtester")
_LIKELIHOODS = (
    "unwahrscheinlich",
    "möglich",
    "wahrscheinlich",
    "sehr wahrscheinlich",
)
_CONDITIONS = (
    "zweites_medium",
    "leitmedium",
    "mandat_in_ueberschrift",
    "medienanfrage",
    "management_genannt",
)
_SPEEDS = (
    "sofort",
    "innerhalb einer Stunde",
    "heute",
    "innerhalb von 24 Stunden",
    "vorbereiten und beobachten",
    "keine Reaktion",
)
_ESCALATION = ("hoch", "mittel", "niedrig")

# The recommendation names a speed: without it "schnell" and "sofort" are the
# same word, which is what the closed speed set exists to prevent.
_RECOMMENDATION_HAS_SPEED = "recommended = 0 OR speed IS NOT NULL"
# A firing that cannot say what it saw is a red mark nobody can act on.
_FIRING_HAS_NOTE = "fired_at IS NULL OR fired_note <> ''"


def upgrade() -> None:
    op.create_table(
        "scenarios",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_KINDS, name="scenario_kind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column(
            "likelihood",
            sa.Enum(
                *_LIKELIHOODS, name="scenario_likelihood", create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column(
            "communication_need", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("brain_version", sa.Integer(), nullable=True),
        sa.CheckConstraint("narrative <> ''", name="ck_scenarios_narrative"),
        sa.UniqueConstraint("issue_id", "kind", name="uq_scenarios_kind"),
    )
    op.create_index("ix_scenarios_issue_id", "scenarios", ["issue_id"])

    op.create_table(
        "scenario_stakeholders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scenario_id",
            sa.Integer(),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "stakeholder_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "scenario_id", "stakeholder_id", name="uq_scenario_stakeholders_once"
        ),
    )
    op.create_index(
        "ix_scenario_stakeholders_scenario_id", "scenario_stakeholders", ["scenario_id"]
    )
    op.create_index(
        "ix_scenario_stakeholders_stakeholder_id",
        "scenario_stakeholders",
        ["stakeholder_id"],
    )

    op.create_table(
        "scenario_triggers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scenario_id",
            sa.Integer(),
            sa.ForeignKey("scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "condition",
            sa.Enum(*_CONDITIONS, name="trigger_condition", create_constraint=True),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_note", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint(_FIRING_HAS_NOTE, name="ck_scenario_triggers_note"),
        sa.UniqueConstraint(
            "scenario_id", "condition", name="uq_scenario_triggers_once"
        ),
    )
    op.create_index(
        "ix_scenario_triggers_scenario_id", "scenario_triggers", ["scenario_id"]
    )

    op.create_table(
        "response_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("benefit", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "escalation",
            sa.Enum(
                *_ESCALATION, name="escalation_potential", create_constraint=True
            ),
            nullable=False,
            server_default="mittel",
        ),
        sa.Column(
            "no_response", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "recommended", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "speed",
            sa.Enum(*_SPEEDS, name="response_speed", create_constraint=True),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("brain_version", sa.Integer(), nullable=True),
        sa.CheckConstraint("label <> ''", name="ck_response_options_label"),
        sa.CheckConstraint("position >= 1", name="ck_response_options_position"),
        sa.CheckConstraint(
            _RECOMMENDATION_HAS_SPEED, name="ck_response_options_speed"
        ),
        sa.UniqueConstraint(
            "issue_id", "position", name="uq_response_options_position"
        ),
    )
    op.create_index("ix_response_options_issue_id", "response_options", ["issue_id"])


def downgrade() -> None:
    op.drop_index("ix_response_options_issue_id", table_name="response_options")
    op.drop_table("response_options")
    op.drop_index("ix_scenario_triggers_scenario_id", table_name="scenario_triggers")
    op.drop_table("scenario_triggers")
    op.drop_index(
        "ix_scenario_stakeholders_stakeholder_id", table_name="scenario_stakeholders"
    )
    op.drop_index(
        "ix_scenario_stakeholders_scenario_id", table_name="scenario_stakeholders"
    )
    op.drop_table("scenario_stakeholders")
    op.drop_index("ix_scenarios_issue_id", table_name="scenarios")
    op.drop_table("scenarios")
    # The CHECKs the Enums emitted go with the tables they were on; nothing else
    # holds them, so dropping the four tables is the whole teardown.
