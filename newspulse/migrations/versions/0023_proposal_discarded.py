"""profile_proposals.discarded_at: remember what the consultant already said no to

A discarded proposal used to be deleted, which left no trace that anyone had
decided anything. The next refresh then read the same about page, found the same
sentence and put the same rejected value back on the review pile — every morning,
for as long as the website said it. A pile that keeps re-asking a question that
was answered is a pile nobody opens.

So a discard stamps this column instead of deleting the row, and the refresh
reads it before proposing. An *accepted* proposal is still deleted: the fact it
became is its own memory.

Keeping the "no" means the UNIQUE (client_id, key) has to go, replaced by the
same uniqueness over the *open* rows only. A refusal is of a sentence and not of
a field: a mandate whose CEO proposal was refused in March and whose website
names someone else in April must be able to hold both rows, or the April finding
could only be filed by deleting the March refusal — and the value the consultant
already said no to would be offered again the moment a page repeated it.

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

#: The uniqueness the proposal store lives by, over the rows still waiting for an
#: answer. Named the same as the constraint it replaces, because it is the same
#: promise: one open proposal per field. What it stops enforcing is uniqueness
#: over the refusals, which is exactly what has to be kept.
_OPEN_ONLY = sa.text("discarded_at IS NULL")


def _table(*, discarded: bool) -> sa.Table:
    """The table as it stands on one side of this migration.

    Batch mode recreates the table to change the primary key to AUTOINCREMENT,
    and each direction has to hand it the shape it is starting *from* — the
    upgrade the 0022 columns under the whole-table UNIQUE, the downgrade those
    plus ``discarded_at`` under the partial index. Spelling it out rather than
    reflecting it is what carries the foreign key, the unique rule and the index
    through the copy: reflection of a SQLite table is lossy enough that alembic
    asks for this.
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
    unique: list[sa.schema.SchemaItem] = [
        sa.Index(
            "uq_profile_proposals_key",
            "client_id",
            "key",
            unique=True,
            sqlite_where=_OPEN_ONLY,
        )
        if discarded
        else sa.UniqueConstraint("client_id", "key", name="uq_profile_proposals_key")
    ]
    if discarded:
        columns.append(sa.Column("discarded_at", sa.DateTime(timezone=True)))
    return sa.Table(
        "profile_proposals",
        sa.MetaData(),
        *columns,
        *unique,
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
        batch.drop_constraint("uq_profile_proposals_key", type_="unique")
    op.create_index(
        "uq_profile_proposals_key",
        "profile_proposals",
        ["client_id", "key"],
        unique=True,
        sqlite_where=_OPEN_ONLY,
    )
    # Rows PRF-01 filed before a source was required. The review page does not
    # draw one — a value nobody can check is not a decision anyone should be
    # asked to make — and no button carries its id, so it can be neither seen nor
    # cleared: exactly the invisible state the review surface exists to end. The
    # refresh stores no more of them, so deleting the ones on file ends it.
    op.execute(sa.text("DELETE FROM profile_proposals WHERE source_url = ''"))


def downgrade() -> None:
    # The old shape holds one row per (client, key) and has nowhere to record a
    # refusal, so the refusals go. Dropping the column would strand them anyway:
    # unstamped, every one of them would read as an open proposal.
    op.execute(sa.text("DELETE FROM profile_proposals WHERE discarded_at IS NOT NULL"))
    with op.batch_alter_table(
        "profile_proposals", copy_from=_table(discarded=True), recreate="always"
    ) as batch:
        batch.drop_index("uq_profile_proposals_key")
        batch.drop_column("discarded_at")
        batch.create_unique_constraint("uq_profile_proposals_key", ["client_id", "key"])
