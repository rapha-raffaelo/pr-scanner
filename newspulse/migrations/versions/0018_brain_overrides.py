"""brain_overrides: the standards, as the agency rewrites them

The blocks ship as files so a fresh install thinks correctly on day one and git
keeps the lineage. This table is the other half of DEC-1 option C: any block can
be overridden in the tool, every change is an event with an author and a time,
and a revert is a row rather than a deletion.

Append-only, so the numbering is the point. ``version`` is portfolio-wide — it
counts changes across every block, not per block — because BRN-03 stamps a
generated text with one integer and reads back what the house believed when the
text was written. Unique for the same reason: two rows sharing a number would
make that lookup ambiguous.

Named 0018 and not 0026: the story was written against a repository that was
expected to have twenty-five migrations by now and has seventeen. Numbering it
0026 would leave a gap that reads as eight lost revisions.

Revision ID: 0018_brain_overrides
Revises: 0017_client_facts
Create Date: 2026-08-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_brain_overrides"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "brain_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        # Nullable on purpose: NULL is "reverted to the shipped default", which
        # is a different answer from the empty string (refused at write time).
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "edited_by", sa.String(length=80), nullable=False, server_default="mensch"
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("version", name="uq_brain_overrides_version"),
    )
    op.create_index("ix_brain_overrides_key", "brain_overrides", ["key"])


def downgrade() -> None:
    op.drop_index("ix_brain_overrides_key", table_name="brain_overrides")
    op.drop_table("brain_overrides")
