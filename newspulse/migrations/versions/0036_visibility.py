"""visibility: the question set a mandate is measured on, and what the models answered

Three tables, and the split between them is the point. A question outlives the
measurements it appears in — the movement panel compares this week against last
week, and both weeks have to name the same question — so it is not a column on a
run. A run outlives its answers for the opposite reason: it is the only row that
can say a provider failed, and a failure has no answer row to hang off.

``visibility_answers.question_id`` is the one foreign key here that does not
cascade. Every other relation in this schema deletes with its parent; this one
restricts, because a question is retired by clearing ``accepted`` precisely so
the answers it produced keep resolving. A cascade would let a click in the
question list silently rewrite what a past measurement said.

That leaves one ordering dependency worth writing down, because nothing in the
app exercises it today: deleting a *client* cascades into both
``visibility_questions`` and ``visibility_runs``, and the questions can only go
once the run cascade has cleared the answers pointing at them. There is no
delete-mandate path in this tool, so no code depends on the engine getting that
order right — but whoever adds one should delete ``visibility_answers``
explicitly first rather than rely on it.

``provider`` is a plain string and not an enum. DEC-2 asks the two assistants
that are already connected and says in as many words that a third is meant to be
added later without touching these tables; a CHECK constraint here would make
that a migration instead of a definition.

Two CHECKs on the answer, and both encode the same distinction the feature exists
for: a rank is 1-based because the page prints it, and an answer that does not
name the mandate cannot rank it. What is deliberately *not* constrained is the
third state — a provider that errored simply has no row, and
``visibility_runs.providers_failed`` is what tells the page to render "nicht
gemessen" rather than "nicht genannt".

Revision ID: 0036_visibility
Revises: 0035_field_usable
Create Date: 2026-08-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_visibility"
down_revision: str | None = "0035_field_usable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the same convention
# 0033_market_signals records: a migration has to keep describing the schema it
# created even after the enum in the code has moved on.
_BANDS = ("marke", "auswahl", "kategorie", "problem")

# The DB-level DEFAULT for the JSON array columns, so a raw INSERT that omits one
# cannot violate NOT NULL. "[]" is valid JSON that SQLite stores verbatim.
_EMPTY_JSON_ARRAY = "[]"


def upgrade() -> None:
    op.create_table(
        "visibility_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "band",
            sa.Enum(*_BANDS, name="visibility_band", create_constraint=True),
            nullable=False,
        ),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "client_id", "text", name="uq_visibility_question_client_text"
        ),
    )
    op.create_index(
        "ix_visibility_questions_client_id", "visibility_questions", ["client_id"]
    )

    op.create_table(
        "visibility_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "providers_asked",
            sa.JSON(),
            nullable=False,
            server_default=_EMPTY_JSON_ARRAY,
        ),
        sa.Column(
            "providers_failed",
            sa.JSON(),
            nullable=False,
            server_default=_EMPTY_JSON_ARRAY,
        ),
    )
    op.create_index("ix_visibility_runs_client_id", "visibility_runs", ["client_id"])
    # Every read of this table asks for the newest run of one mandate, and the
    # spend guard asks it before deciding whether to spend anything at all.
    op.create_index("ix_visibility_runs_ran_at", "visibility_runs", ["ran_at"])

    op.create_table(
        "visibility_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("visibility_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.Integer(),
            sa.ForeignKey("visibility_questions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("named", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column(
            "companies", sa.JSON(), nullable=False, server_default=_EMPTY_JSON_ARRAY
        ),
        sa.Column("rivals", sa.JSON(), nullable=False, server_default=_EMPTY_JSON_ARRAY),
        sa.Column(
            "sources", sa.JSON(), nullable=False, server_default=_EMPTY_JSON_ARRAY
        ),
        sa.UniqueConstraint(
            "run_id", "question_id", "provider", name="uq_visibility_answer_cell"
        ),
        sa.CheckConstraint(
            "position IS NULL OR position >= 1", name="ck_visibility_answer_position"
        ),
        sa.CheckConstraint(
            "named OR position IS NULL", name="ck_visibility_answer_unnamed_rank"
        ),
    )
    op.create_index("ix_visibility_answers_run_id", "visibility_answers", ["run_id"])
    op.create_index(
        "ix_visibility_answers_question_id", "visibility_answers", ["question_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_visibility_answers_question_id", table_name="visibility_answers")
    op.drop_index("ix_visibility_answers_run_id", table_name="visibility_answers")
    op.drop_table("visibility_answers")
    op.drop_index("ix_visibility_runs_ran_at", table_name="visibility_runs")
    op.drop_index("ix_visibility_runs_client_id", table_name="visibility_runs")
    op.drop_table("visibility_runs")
    op.drop_index(
        "ix_visibility_questions_client_id", table_name="visibility_questions"
    )
    op.drop_table("visibility_questions")
