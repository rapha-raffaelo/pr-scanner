"""profile_proposals.discarded_at: remember what the consultant already said no to

A discarded proposal used to be deleted, which left no trace that anyone had
decided anything. The next refresh then read the same about page, found the same
sentence and put the same rejected value back on the review pile — every morning,
for as long as the website said it. A pile that keeps re-asking a question that
was answered is a pile nobody opens.

So a discard stamps this column instead of deleting the row, and the refresh
reads it before proposing. An *accepted* proposal is still deleted: the fact it
became is its own memory.

Revision ID: 0023_proposal_discarded
Revises: 0022_profile_proposals
Create Date: 2026-08-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_proposal_discarded"
down_revision: str | None = "0022_profile_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profile_proposals") as batch:
        batch.add_column(
            sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("profile_proposals") as batch:
        batch.drop_column("discarded_at")
