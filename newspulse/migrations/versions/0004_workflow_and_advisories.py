"""author, triage state, competitor flag, advisories

Four additions that together turn the archive from a read-only feed into a
working tool:

* ``articles.author`` — the byline when a feed supplies one. Knowing which
  journalist covers a client repeatedly is how a media list is built; it was
  previously parsed and discarded.
* ``analyses.triage_state`` — where a piece of coverage stands in the morning
  workflow. Per (article, client), because one story can be handled for one
  mandate and still open for another.
* ``clients.is_competitor`` — a monitored company that is never reported *to*,
  which is what share-of-voice needs.
* ``advisories`` — generated sets of suggested PR actions, kept as history.

Every column is NOT NULL with a DB-level default, so rows written by a path that
does not know about them stay valid.

Revision ID: 0004_workflow_and_advisories
Revises: 0003_index_published_at
Create Date: 2026-07-27
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_workflow_and_advisories"
down_revision: str | None = "0003_index_published_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIAGE_STATES = ("neu", "gelesen", "erledigt", "markiert")


def upgrade() -> None:
    # batch_alter_table so SQLite's lack of full ALTER support is handled by a
    # table rebuild — the same approach as 0002.
    with op.batch_alter_table("articles") as batch:
        batch.add_column(sa.Column("author", sa.String(255), nullable=True))

    with op.batch_alter_table("analyses") as batch:
        batch.add_column(
            sa.Column(
                "triage_state",
                sa.Enum(*_TRIAGE_STATES, name="triagestate", create_constraint=True),
                nullable=False,
                server_default="neu",
            )
        )

    with op.batch_alter_table("clients") as batch:
        batch.add_column(
            sa.Column(
                "is_competitor",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    op.create_table(
        "advisories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("covered_days", sa.Integer(), nullable=False),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False),
        sa.Column("suggestions", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_advisories_client_id", "advisories", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_advisories_client_id", table_name="advisories")
    op.drop_table("advisories")
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("is_competitor")
    # As in 0006: the CHECK that ``sa.Enum(create_constraint=True)`` emits is
    # named after the column and has to be dropped with it. SQLite has no ALTER
    # for either, so batch mode rebuilds the table from the reflected definition
    # — and carried a CHECK on ``triage_state`` into a table that no longer had
    # the column.
    with op.batch_alter_table("analyses") as batch:
        # triagestate, not triage_state: the CHECK is named after the
        # Enum, which is not always spelled like the column it sits on.
        batch.drop_constraint("triagestate", type_="check")
        batch.drop_column("triage_state")
    with op.batch_alter_table("articles") as batch:
        batch.drop_column("author")
