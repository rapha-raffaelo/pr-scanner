"""the contact's position: who they are at the masthead, not what they cover

"was wir noch haben ist die Position im Kontaktbuch, das sollte hinzugefügt
werden. hier schreiben wir am besten immer die Position im Unternehmen."

The book already knew the masthead and the beat, and neither answers the
question a pitch turns on: two people cover banking at the same paper, and the
one who runs the desk is approached differently from the one who files to them.
Until now that went into ``notes`` if it was recorded at all — prose that
nothing can sort by, filter on, or put in a letter's salutation.

Non-null with an empty default, like every other optional string on the table:
an entry that predates this column has no position rather than an unknown one,
and the page renders the absence instead of a blank line pretending to be a
value.

Revision ID: 0049_contact_position
Revises: 0048_decision_packet
Create Date: 2026-09-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_contact_position"
down_revision: str | None = "0048_decision_packet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(
            sa.Column(
                "position",
                sa.String(length=200),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("position")
