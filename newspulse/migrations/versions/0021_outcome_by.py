"""outreach.outcome_by: who recorded the outcome

The ledger stored *when* an outcome was recorded and never *who by*, which was
harmless while only a person could record one. The mailbox sync can now write an
``antwort`` itself, and both pages that draw an outcome drew every one of them as
something a consultant typed — a sentence nobody said, in the one place in this
tool that exists to be audited.

Inferring the author from state, note and timestamp was the alternative, and it
is re-derived on every render: the day a retention rule deletes the reply row the
inference reads, every machine-written line silently becomes a human's again. So
the fact is stored, once, the way ``released_by`` already stores the other half
of the same question.

The backfill runs that inference exactly once, against data that can no longer
change: everything already on file is a person's entry, except an ``antwort``
with no note standing at the very moment of a stored reply, which is the only
shape the sync is able to write.

Revision ID: 0021_outcome_by
Revises: 0020_outreach_replies
Create Date: 2026-08-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_outcome_by"
down_revision: str | None = "0020_outreach_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same width ``released_by`` uses: this holds a token ("mensch", "postfach"),
# not a name, and the two columns answer the same question about one row.
_AUTHOR_LENGTH = 80

# The two tokens themselves, spelled here rather than imported from the models:
# a migration is a record of what was run, and it has to keep meaning the same
# thing on the day the application renames its constant.
_BY_HAND = "mensch"
_BY_MAILBOX = "postfach"


def upgrade() -> None:
    # Plain ``add_column`` rather than the ``batch_alter_table`` 0019 used on this
    # same table: adding a column is one of the few ALTERs SQLite does natively,
    # and batch mode would drop and recreate ``outreach`` — which now has a child
    # table hanging off it (``outreach_replies``, ON DELETE CASCADE, added in
    # 0020). Recreating a parent under a cascade is not a risk worth taking for a
    # statement SQLite can execute as written.
    op.add_column(
        "outreach",
        sa.Column(
            "outcome_by",
            sa.String(length=_AUTHOR_LENGTH),
            nullable=False,
            server_default="",
        ),
    )
    # Everything recorded before this column existed was typed by a person: the
    # sync that can write one ships in the same release as the table it reads.
    op.execute(
        sa.text(
            "UPDATE outreach SET outcome_by = :hand WHERE outcome_at IS NOT NULL"
        ).bindparams(hand=_BY_HAND)
    )
    # Except, for an installation that ran 0020 before this one: an ``antwort``
    # with no note, stamped at the exact moment of a reply on file, is the one
    # thing ``record_reply`` is able to write and nothing a person can produce by
    # accident.
    op.execute(
        sa.text(
            "UPDATE outreach SET outcome_by = :mailbox "
            "WHERE outcome_at IS NOT NULL AND state = 'antwort' AND outcome_note = '' "
            "AND EXISTS (SELECT 1 FROM outreach_replies r "
            "            WHERE r.outreach_id = outreach.id "
            "              AND r.received_at = outreach.outcome_at)"
        ).bindparams(mailbox=_BY_MAILBOX)
    )


def downgrade() -> None:
    # SQLite has had DROP COLUMN since 3.35 and every Python this runs on ships a
    # newer one; the same reasoning as above applies to not recreating the table.
    op.drop_column("outreach", "outcome_by")
