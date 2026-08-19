"""profile_proposals.discarded_at: remember what the consultant already said no to

A discarded proposal used to be deleted, which left no trace that anyone had
decided anything. The next refresh then read the same about page, found the same
sentence and put the same rejected value back on the review pile — every morning,
for as long as the website said it. A pile that keeps re-asking a question that
was answered is a pile nobody opens.

So a discard stamps this column instead of deleting the row, and the refresh
reads it before proposing. An *accepted* proposal is still deleted: the fact it
became is its own memory.

The table is also rebuilt with AUTOINCREMENT. The review page's buttons carry
row ids, and a refresh replaces a client's proposals by deleting and re-inserting
them; a plain SQLite rowid is reused after that delete, so yesterday's id can
come back attached to this morning's finding and a stale tab would accept a value
nobody read.

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

def _table(*, discarded: bool) -> sa.Table:
    """The table as it stands on one side of this migration.

    Batch mode recreates the table to change the primary key to AUTOINCREMENT,
    and each direction has to hand it the shape it is starting *from* — the
    upgrade the 0022 columns, the downgrade those plus ``discarded_at``.
    Spelling it out rather than reflecting it is what carries the foreign key,
    the unique constraint and the index through the copy: reflection of a SQLite
    table is lossy enough that alembic asks for this.
    """
    columns: list[sa.Column] = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_title", sa.Text(), nullable=False, server_default=""),
        sa.Column("previous_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "proposed_by", sa.String(length=80), nullable=False, server_default=""
        ),
    ]
    if discarded:
        columns.append(sa.Column("discarded_at", sa.DateTime(timezone=True)))
    return sa.Table(
        "profile_proposals",
        sa.MetaData(),
        *columns,
        sa.UniqueConstraint("client_id", "key", name="uq_profile_proposals_key"),
        sa.Index("ix_profile_proposals_client_id", "client_id"),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "profile_proposals",
        copy_from=_table(discarded=False),
        recreate="always",
        table_kwargs={"sqlite_autoincrement": True},
    ) as batch:
        batch.add_column(
            sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "profile_proposals", copy_from=_table(discarded=True), recreate="always"
    ) as batch:
        batch.drop_column("discarded_at")
