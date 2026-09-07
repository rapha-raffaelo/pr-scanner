"""the mandate signs a letter off before the journalist ever sees it

"bevor die nachrichten an die journalisten können müssen sie erst mit dem
Kunden abgestimmt werden. das sollten wir berücksichtigen."

A letter goes out in the mandate's name, quoting the mandate's position. The
consultant's release was the only signature on the row, so the tool could put
a claim in a journalist's inbox that the client had never read.

Two columns rather than one flag: who agreed, and when. "Der Kunde hat
zugestimmt" is not a fact anybody can check three months later; "Frau Berg,
7.9." is. And they stay separate from released_at/released_by, which answer a
different question asked of a different person — may we say this in your name,
versus does this go out now.

Nullable: every letter written before this column existed has no client
sign-off recorded, which is the truth about it. They are refused by the send
path until somebody records one, which is the point.

Revision ID: 0053_client_signoff
Revises: 0052_contact_name_parts
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_client_signoff"
down_revision: str | None = "0052_contact_name_parts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outreach") as batch:
        batch.add_column(sa.Column("client_ok_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("client_ok_by", sa.String(length=120), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("outreach") as batch:
        batch.drop_column("client_ok_by")
        batch.drop_column("client_ok_at")
