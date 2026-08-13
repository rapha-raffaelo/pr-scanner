"""impulse_note: why this mandate has no positioning, on the record

The reason a draft did not happen lived in a dict in the web process, written
only by the button. The nightly sweep never touched it, so a mandate that the
06:10 run found nothing for showed no explanation at nine — and the report came
back six times as "es funktioniert immer noch nicht".

Stored on the client because it is the answer to a question asked about the
client, and because it has to survive the hours between the sweep and the person
opening the page.

Revision ID: 0014_impulse_note
Revises: 0013_contacts
Create Date: 2026-08-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_impulse_note"
down_revision: str | None = "0013_contacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("impulse_note", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("impulse_checked_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("impulse_checked_at")
        batch.drop_column("impulse_note")
