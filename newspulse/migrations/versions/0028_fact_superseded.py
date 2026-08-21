"""client_facts: room for the value a kick-off answer replaced (DEC-2)

DEC-2 option A: an answer from the client or the consultant replaces the
researched value on accept, and the old value stays visible as what the web said.
That needs the profile to hold a superseded value, which it did not.

Five columns rather than one, because a superseded value without its provenance
is worse than none: the whole point of keeping it is that the page can say "die
Website sagte X" and link the page it was read from while the disagreement
stands.

Nullable/defaulted throughout, so every existing row upgrades to "nothing was
superseded here" without a data migration.

Revision ID: 0028_fact_superseded
Revises: 0027_onboarding_answers
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_fact_superseded"
down_revision: str | None = "0027_onboarding_answers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "client_facts",
        sa.Column("superseded_value", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "client_facts",
        sa.Column("superseded_source_url", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "client_facts",
        sa.Column(
            "superseded_source_title", sa.Text(), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "client_facts",
        sa.Column(
            "superseded_filled_by",
            sa.String(length=80),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "client_facts",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "superseded_at",
        "superseded_filled_by",
        "superseded_source_title",
        "superseded_source_url",
        "superseded_value",
    ):
        op.drop_column("client_facts", column)
