"""outreach_replies: what the journalist wrote back

One letter can collect several answers — a reply, a follow-up question, the note
two weeks later that the piece is running — so the replies are their own table
rather than a column that would hold the last one and lose the rest.

Three decisions are in the schema rather than in the code that writes it:

* ``gmail_message_id`` is UNIQUE. It is Google's id for one message in one
  mailbox, so a second row carrying it would be the same mail filed twice. That
  constraint is what makes the daily sync idempotent even if the code above it
  ever forgets to check.
* ``ON DELETE CASCADE`` on the letter. A journalist's own words must not outlive
  the letter they answered, and a nullable link would leave orphaned reply text
  in the database with nothing to say what it was a reply to.
* ``received_at`` and ``fetched_at`` are both stored. When the mail was written
  and when this tool took a copy of somebody else's data are two different facts,
  and a retention rule later needs the second one.

Revision ID: 0020_outreach_replies
Revises: 0019_outreach_gmail
Create Date: 2026-08-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_outreach_replies"
down_revision: str | None = "0019_outreach_gmail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Google's message ids are opaque hex strings well under 30 characters; 120 is
# the same room 0019 left for the thread and draft ids, for the same reason.
_ID_LENGTH = 120

# A display name off a From header. Long enough for "Dr. Marlene Kühn-Bergmann
# (Handelsblatt Redaktion Energie)" and bounded, so a header nobody sanitised
# cannot grow a row without limit.
_NAME_LENGTH = 200

# The maximum length of an email address per RFC 5321, which is also what the
# contact book's column uses.
_ADDRESS_LENGTH = 320


def upgrade() -> None:
    op.create_table(
        "outreach_replies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "outreach_id",
            sa.Integer(),
            sa.ForeignKey("outreach.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gmail_message_id", sa.String(length=_ID_LENGTH), nullable=False),
        sa.Column(
            "from_name", sa.String(length=_NAME_LENGTH), nullable=False, server_default=""
        ),
        sa.Column(
            "from_email",
            sa.String(length=_ADDRESS_LENGTH),
            nullable=False,
            server_default="",
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "gmail_message_id", name="uq_outreach_replies_gmail_message_id"
        ),
    )
    # "Every reply to this letter" is the only way this table is ever read — the
    # contact's file asks it once per letter on the timeline.
    op.create_index(
        "ix_outreach_replies_outreach_id", "outreach_replies", ["outreach_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_outreach_replies_outreach_id", table_name="outreach_replies")
    op.drop_table("outreach_replies")
