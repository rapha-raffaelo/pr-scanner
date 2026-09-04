"""the register's model-written reasons say which standards produced them

``IssueSignal.reason`` is the sentence explaining why a piece belongs to a
running matter, and since DEC-4 a model writes most of them. Every other
generated text in the tool carries the brain version it was written under;
this one did not, so a consultant reading "Derselbe Vorwurf, neu formuliert"
could not tell which standards produced it — and the guard in test_brain.py
caught the module the moment it began composing and persisting in one place.

Nullable, like ``Outreach.brain_version`` and for the same reason: the row is
written by two hands, and a consultant attaching a piece by hand carries no
version. NULL means a person wrote that sentence, which is a stronger claim
than any stamp.

Revision ID: 0050_issue_signal_stamp
Revises: 0049_contact_position
Create Date: 2026-09-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_issue_signal_stamp"
down_revision: str | None = "0049_contact_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issue_signals") as batch:
        batch.add_column(sa.Column("brain_version", sa.Integer(), nullable=True))
        batch.create_index(
            "ix_issue_signals_brain_version", ["brain_version"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("issue_signals") as batch:
        batch.drop_index("ix_issue_signals_brain_version")
        batch.drop_column("brain_version")
