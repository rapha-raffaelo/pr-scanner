"""an occasion a person declined leaves the rail, and stays on the record

"hier wäre es gut wenn du ein Update machst wo kreuze zum schliessen und
verwerfen der vorschläge sind."

The Texte rail is a row of proposals — the occasions the radar found. It had
no way to decline one, so a proposal a consultant had looked at and rejected
sat in the row every morning until it aged out, and the one worth reading was
three cards to the right.

A timestamp, not a delete, for the same reason ``Analysis.dismissed_at`` is
one: the texts written from an occasion hang on its row, and a released press
release must not disappear because its occasion was tidied away. The client
report reads these rows back — a draft written in a period was written in it.

Revision ID: 0051_angle_dismissed
Revises: 0050_issue_signal_stamp
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_angle_dismissed"
down_revision: str | None = "0050_issue_signal_stamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("angles") as batch:
        batch.add_column(sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("angles") as batch:
        batch.drop_column("dismissed_at")
