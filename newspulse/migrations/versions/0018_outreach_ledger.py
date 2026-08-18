"""outreach ledger: who released a letter, and what came back

The loop from impulse to journalist ended at a Kopieren button: the consultant
pasted the letter into his own mail client, sent it, and the tool never learned
that it had happened. Everything downstream was missing for that one reason — no
relationship history, no audit trail under the release the product's own L5 claim
rests on, no way to connect a piece of coverage to the pitch that produced it.

Six columns, and the shape of them is the argument. ``state`` is a closed set with
a CHECK behind it, and the state everybody asks for is deliberately not in it:
"ohne Reaktion" is ``raus`` plus fourteen days, derived at read, because a stored
silence is a fact nobody entered. ``released_at`` is the field that cannot go
backwards — an outcome moves the state on, but the release stays where it was.
``contact_id`` is resolved once, at release, and is nullable with SET NULL: the
recipient's name and masthead are on the row already, so a deleted contact costs
the link and never the record of who was written to.

Existing rows become ``entwurf``. They were written before releasing was a thing
anyone could do, so none of them can honestly claim to have gone out.

Revision ID: 0018_outreach_ledger
Revises: 0017_client_facts
Create Date: 2026-08-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_outreach_ledger"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors newspulse.models.OutreachState. Spelled out rather than imported: a
# migration describes the schema at one point in history, and following the enum
# as it grows would silently rewrite what this revision claims to have done.
_STATES = ("entwurf", "raus", "antwort", "absage", "veroeffentlicht")


def upgrade() -> None:
    with op.batch_alter_table("outreach") as batch:
        batch.add_column(
            sa.Column(
                "contact_id",
                sa.Integer(),
                sa.ForeignKey(
                    "contacts.id",
                    ondelete="SET NULL",
                    # Named because batch mode refuses an anonymous constraint:
                    # SQLite cannot ALTER one in, so alembic has to be able to
                    # name it in the rebuilt table.
                    name="fk_outreach_contact_id",
                ),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "state",
                # The CHECK is added below rather than by the type. Adding the
                # ``contact_id`` foreign key forces batch mode to rebuild the
                # table, and a self-creating Enum constraint is then emitted twice
                # into the same CREATE TABLE — once by the column, once by the
                # rebuild — which SQLite accepts and nobody wants to read.
                sa.Enum(*_STATES, name="outreachstate", create_constraint=False),
                nullable=False,
                # Every row that predates the ledger is a draft: releasing did not
                # exist, so no stored letter can claim a human sent it.
                server_default="entwurf",
            )
        )
        batch.create_check_constraint(
            "outreachstate", sa.column("state").in_(_STATES)
        )
        batch.add_column(sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("released_by", sa.String(length=80), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("outcome_note", sa.Text(), nullable=False, server_default="")
        )
    op.create_index("ix_outreach_contact_id", "outreach", ["contact_id"])
    op.create_index("ix_outreach_released_at", "outreach", ["released_at"])


def downgrade() -> None:
    op.drop_index("ix_outreach_released_at", table_name="outreach")
    op.drop_index("ix_outreach_contact_id", table_name="outreach")
    with op.batch_alter_table("outreach") as batch:
        # Before the column it talks about: SQLite rebuilds the table to drop a
        # column, and a CHECK left pointing at a column that is going away makes
        # the rebuilt CREATE TABLE invalid.
        batch.drop_constraint("outreachstate", type_="check")
        batch.drop_column("outcome_note")
        batch.drop_column("outcome_at")
        batch.drop_column("released_by")
        batch.drop_column("released_at")
        batch.drop_column("state")
        batch.drop_column("contact_id")
