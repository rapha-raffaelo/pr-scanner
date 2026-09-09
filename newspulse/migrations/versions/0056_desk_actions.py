"""the contract figure on a mandate, and a ledger for work done elsewhere

The Desk answers "how many actions did we promise this quarter, and how many
have we delivered". The second half is mostly derivable — every Outreach, Asset
and Report carries a ``released_at`` — but only mostly: a background call with a
correspondent, a briefing before an interview, a visit to an editorial office.
None of that touches this tool, and counting only what the tool released would
show the agency doing consistently less than it does. Against a contractual
figure that is not a neutral error.

``actions_per_quarter`` is nullable on purpose. Zero is a mandate that owes
nothing; NULL is a mandate nobody has entered a figure for, and the Desk must
not draw the second as behind on a commitment of none.

Renumbered from 0055 before merge: the branch that adds the matcher's narrowing
terms had already claimed that number with 0054 as its parent, and two
revisions naming one parent give alembic two heads and make `upgrade head`
fail. Safe to renumber because this one has never been deployed.

Revision ID: 0056_desk_actions
Revises: 0055_client_match_terms
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_desk_actions"
down_revision: str | None = "0055_client_match_terms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("actions_per_quarter", sa.Integer(), nullable=True))

    op.create_table(
        "client_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        # ``timezone=True``, matching the model's UTCDateTime and every sibling
        # revision back to 0040.
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_client_actions_client_id", "client_actions", ["client_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_client_actions_client_id", table_name="client_actions")
    op.drop_table("client_actions")
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("actions_per_quarter")
