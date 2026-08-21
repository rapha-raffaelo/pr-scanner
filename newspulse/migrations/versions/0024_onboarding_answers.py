"""onboarding_answers: the kick-off questionnaire, answer by answer

Its own table rather than rows in ``client_facts``, because a raw answer and an
adopted fact are different things: the questionnaire records what was said in the
call, and only a separate, deliberate accept turns any of it into policy.

``skipped`` is a column rather than a sentinel value so the three states stay
distinct in the schema itself — answered, deliberately passed over, and never
asked (no row at all).

Revision ID: 0024_onboarding_answers
Revises: 0017_client_facts
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_onboarding_answers"
# The revision id follows the story's numbering; the parent is whatever is
# actually at head, so the chain stays contiguous even though the numbers do not.
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        # The literal is spelled out rather than imported: a migration is a
        # historical record and must keep saying what it said the day it ran, even
        # after ``models.ANSWERED_BY_DEFAULT`` becomes a different name (DEC-1
        # option B). That constant is where the application reads it from.
        sa.Column(
            "answered_by", sa.String(length=80), nullable=False, server_default="Berater"
        ),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("client_id", "key", name="uq_onboarding_answers_key"),
    )
    op.create_index(
        "ix_onboarding_answers_client_id", "onboarding_answers", ["client_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_answers_client_id", table_name="onboarding_answers")
    op.drop_table("onboarding_answers")
