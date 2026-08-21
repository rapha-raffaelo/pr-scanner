"""outreach: the Gmail thread a released letter opened

DEC-4 locked option C — RauteOS composes the letter in Gmail and sends it — so
every released letter that went out this way has a conversation attached to it.
Three columns hold Google's own ids for it: the draft it was composed as, the
thread it opened, and the message that actually left.

Why the ids are stored at all, given the letter's text is already on the row: the
thread id is what turns OUT-05's reply matching from a guess into a fact. Without
an outgoing message of ours in the conversation, a reply can only be matched by
recipient, subject and a time window, which is wrong often enough to file a
journalist's answer against the wrong pitch.

Empty strings rather than NULL, and the same on every existing row: a letter
written before this revision was never sent from here, and "" reads as that
everywhere without a caller having to tell None and "" apart.

Revision ID: 0019_outreach_gmail
Revises: 0018_outreach_ledger
Create Date: 2026-08-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_outreach_gmail"
down_revision: str | None = "0018_outreach_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Google's ids are opaque hex strings, comfortably under 30 characters today.
# 120 is room for a format change without a migration, and still short enough to
# be an index-able column on any backend.
_ID_LENGTH = 120

_COLUMNS = ("gmail_draft_id", "gmail_thread_id", "gmail_message_id")


def upgrade() -> None:
    with op.batch_alter_table("outreach") as batch:
        for name in _COLUMNS:
            batch.add_column(
                sa.Column(
                    name,
                    sa.String(length=_ID_LENGTH),
                    nullable=False,
                    # Every row that predates this revision went out — if it went
                    # out at all — through the consultant's own mail client, so
                    # none of them has a thread this tool started.
                    server_default="",
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("outreach") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
