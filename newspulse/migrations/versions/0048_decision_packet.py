"""the decision packet: its statements, their evidence, its contradictions, its gaps

Five tables carry RIS-05, and each holds one of the paper's disciplines at the
schema so no future writer has to remember it:

**decision_packets** — one paper per press of the button, hanging on an issue or
on a crisis (the CHECK says exactly one). Deliberately no UNIQUE over the
anchor: "ein neues Papier zum selben Issue ersetzt das alte nicht, sondern tritt
daneben", and two papers a week apart are the record of how the reading changed.
The second CHECK ties a recorded decision to its words and to the person who
took it, because that is the question asked afterwards and a decision without a
name answers two thirds of it.

**decision_statements** — one sentence per row, in one of the three parts.
``source_rank`` is CHECKed to stand on ``belegt`` rows and on no others: a rank
on an unconfirmed sentence claims an authority nobody gave it, and a belegt
sentence without one hides where it sits in the Quellenordnung.

**decision_evidence** — the stored row a belegt sentence resolves to. The id is
kept and the text is *copied*: the paper is the record of what a decision rested
on, so a headline re-titled or a piece of coverage dismissed afterwards must not
change what the paper says it said. No foreign key into the six tables it can
point at, on purpose — a CASCADE there would delete the evidence out from under
a stored paper.

**decision_contradictions** — both sides, as NOT NULL columns. That is the
acceptance itself: a contradiction with only one side is not reported, because a
reported contradiction that cannot name what it contradicts is believed in a
crisis.

**decision_gaps** — the named gaps found in the material, frozen onto the paper.
The decider and the deadline are columns on the packet and are read live, so
naming them can get them filled in.

Numbering follows the story (RIS-05 was specified as ``0048_decision_packet``);
the chain, not the number, is what orders a migration.

Revision ID: 0048_decision_packet
Revises: 0047_scenarios
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_decision_packet"
down_revision: str | None = "0047_scenarios"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the convention every
# migration here keeps: a migration has to keep describing the schema it
# created even after the constants in the code moved on.
_SECTIONS = ("belegt", "unbestätigt", "offen")
_EVIDENCE_KINDS = ("beitrag", "analyse", "profil", "marktsignal", "mail", "text")
_SOURCE_RANKS = (
    "bestätigte interne Angabe",
    "Behörde oder Originaldokument",
    "verifizierter Medienbericht",
    "alles Übrige",
)
_GAP_KINDS = (
    "sprecher",
    "krisenkontakt",
    "betroffenenzahl",
    "entscheider",
    "frist",
)

# The same widths the models declare. ``80`` is the name ceiling every other
# "who did this" column in this schema uses.
_NAME = 80
_LABEL = 300

# A paper hangs on exactly one occasion.
_ONE_ANCHOR = "(issue_id IS NULL) <> (crisis_id IS NULL)"
# What was decided, by whom — recorded together or not at all.
_DECISION_HAS_PERSON = "decided_at IS NULL OR (decision <> '' AND decided_by <> '')"
# Belegt and only belegt carries a rank in the Quellenordnung.
_RANK_IS_BELEGT = "(section = 'belegt') = (source_rank IS NOT NULL)"


def upgrade() -> None:
    op.create_table(
        "decision_packets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "decision_maker", sa.String(_NAME), nullable=False, server_default=""
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(_NAME), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(_NAME), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("brain_version", sa.Integer(), nullable=True),
        sa.CheckConstraint(_ONE_ANCHOR, name="ck_decision_packets_one_anchor"),
        sa.CheckConstraint("situation <> ''", name="ck_decision_packets_situation"),
        sa.CheckConstraint(
            _DECISION_HAS_PERSON, name="ck_decision_packets_decision"
        ),
    )
    op.create_index("ix_decision_packets_client_id", "decision_packets", ["client_id"])
    op.create_index("ix_decision_packets_issue_id", "decision_packets", ["issue_id"])
    op.create_index("ix_decision_packets_crisis_id", "decision_packets", ["crisis_id"])

    op.create_table(
        "decision_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "packet_id",
            sa.Integer(),
            sa.ForeignKey("decision_packets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section",
            sa.Enum(*_SECTIONS, name="packet_section", create_constraint=True),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "source_rank",
            sa.Enum(*_SOURCE_RANKS, name="source_rank", create_constraint=True),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("text <> ''", name="ck_decision_statements_text"),
        sa.CheckConstraint("position >= 1", name="ck_decision_statements_position"),
        sa.CheckConstraint(_RANK_IS_BELEGT, name="ck_decision_statements_rank"),
        sa.UniqueConstraint(
            "packet_id", "position", name="uq_decision_statements_position"
        ),
    )
    op.create_index(
        "ix_decision_statements_packet_id", "decision_statements", ["packet_id"]
    )

    op.create_table(
        "decision_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "statement_id",
            sa.Integer(),
            sa.ForeignKey("decision_statements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_EVIDENCE_KINDS, name="evidence_kind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(_LABEL), nullable=False),
        sa.Column("source", sa.String(_LABEL), nullable=False, server_default=""),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False, server_default=""),
        sa.CheckConstraint("label <> ''", name="ck_decision_evidence_label"),
        sa.UniqueConstraint(
            "statement_id", "kind", "ref_id", name="uq_decision_evidence_once"
        ),
    )
    op.create_index(
        "ix_decision_evidence_statement_id", "decision_evidence", ["statement_id"]
    )

    op.create_table(
        "decision_contradictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "packet_id",
            sa.Integer(),
            sa.ForeignKey("decision_packets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        # Two enum columns of one value set on one table, so each names its own
        # CHECK: two constraints of one name on one table is a schema nobody
        # can alter afterwards.
        sa.Column(
            "left_kind",
            sa.Enum(
                *_EVIDENCE_KINDS,
                name="contradiction_left_kind",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("left_ref_id", sa.Integer(), nullable=False),
        sa.Column("left_label", sa.String(_LABEL), nullable=False),
        sa.Column("left_source", sa.String(_LABEL), nullable=False, server_default=""),
        sa.Column(
            "right_kind",
            sa.Enum(
                *_EVIDENCE_KINDS,
                name="contradiction_right_kind",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("right_ref_id", sa.Integer(), nullable=False),
        sa.Column("right_label", sa.String(_LABEL), nullable=False),
        sa.Column(
            "right_source", sa.String(_LABEL), nullable=False, server_default=""
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("note <> ''", name="ck_decision_contradictions_note"),
        sa.CheckConstraint(
            "left_label <> '' AND right_label <> ''",
            name="ck_decision_contradictions_sides",
        ),
        sa.CheckConstraint(
            "position >= 1", name="ck_decision_contradictions_position"
        ),
        sa.UniqueConstraint(
            "packet_id", "position", name="uq_decision_contradictions_position"
        ),
    )
    op.create_index(
        "ix_decision_contradictions_packet_id",
        "decision_contradictions",
        ["packet_id"],
    )

    op.create_table(
        "decision_gaps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "packet_id",
            sa.Integer(),
            sa.ForeignKey("decision_packets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_GAP_KINDS, name="gap_kind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 1", name="ck_decision_gaps_position"),
        sa.UniqueConstraint("packet_id", "kind", name="uq_decision_gaps_once"),
    )
    op.create_index("ix_decision_gaps_packet_id", "decision_gaps", ["packet_id"])


def downgrade() -> None:
    op.drop_index("ix_decision_gaps_packet_id", table_name="decision_gaps")
    op.drop_table("decision_gaps")
    op.drop_index(
        "ix_decision_contradictions_packet_id", table_name="decision_contradictions"
    )
    op.drop_table("decision_contradictions")
    op.drop_index(
        "ix_decision_evidence_statement_id", table_name="decision_evidence"
    )
    op.drop_table("decision_evidence")
    op.drop_index(
        "ix_decision_statements_packet_id", table_name="decision_statements"
    )
    op.drop_table("decision_statements")
    op.drop_index("ix_decision_packets_crisis_id", table_name="decision_packets")
    op.drop_index("ix_decision_packets_issue_id", table_name="decision_packets")
    op.drop_index("ix_decision_packets_client_id", table_name="decision_packets")
    op.drop_table("decision_packets")
    # The CHECKs the Enums emitted go with the tables they were on; nothing else
    # holds them, so dropping the five tables is the whole teardown.
