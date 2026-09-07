"""an opportunity carries the forward look written with its verdict

DEC-3 B puts a look-ahead on the dossier — "2 bis 7 Tage: Verbände
positionieren sich" — and DEC-3 is explicit about where it may be produced:
once, at detection, stored with the opportunity. Never at page load. A dossier
is opened four times on the morning a window is closing, and a tile recomputed
on every view would charge a model call for looking at what is already known.

So two columns rather than a read-time call. ``outlook`` is the estimate,
``outlook_at`` is when it was made — shown beside it, because an estimate whose
age is invisible gets read like the evidenced lines next to it.

Both are additive and both have an honest empty value: every row written before
this revision carries ``[]`` and NULL, and the tile says the look-ahead is not
on file rather than inventing a calendar.

Revision ID: 0051_newsjack_outlook
Revises: 0053_client_signoff

Renumbered from 0051 on merge: main had grown three revisions (0051 to 0053)
while this branch was open, and two revisions naming 0050 as their parent give
alembic two heads and make `upgrade head` fail. Safe to renumber because this
one has never been deployed — nothing carries its old id in an alembic_version
column anywhere.
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_newsjack_outlook"
down_revision: str | None = "0053_client_signoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("newsjack_opportunities") as batch:
        batch.add_column(
            sa.Column(
                "outlook",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        # ``timezone=True``, matching the model's ``UTCDateTime`` and every
        # sibling revision back to 0040. SQLite ignores it either way, so the
        # difference is invisible here and shows up as a schema an autogenerate
        # diff calls drifted the first time this runs anywhere else.
        batch.add_column(
            sa.Column("outlook_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("newsjack_opportunities") as batch:
        batch.drop_column("outlook_at")
        batch.drop_column("outlook")
