"""outreach ledger: the human act at the end of the pipeline becomes a record

The report claims coverage "aus eigener Ansprache". That claim is only allowed to
rest on a letter somebody actually released — a draft nobody sent cannot have
caused a piece of press — so the ledger needs a place to record the release
before the metric layer can join against it.

Three columns, deliberately: a null ``released_at`` and an empty ``released_by``
are what make "still a draft" a fact rather than an inference, and ``state``
carries what happened afterwards, which no timestamp can derive. Existing rows
become drafts, which is exactly what they are.

This is the revision id the outreach ledger feature reserves, and it is taken here
rather than branched beside it: a second revision off ``0017_client_facts`` would
leave alembic with two heads and add ``released_at`` twice. The remaining ledger
columns — ``contact_id``, ``outcome_at``, ``outcome_note`` — belong to the
functions that write them and arrive with those.

Revision ID: 0018_outreach_ledger
Revises: 0017_client_facts
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_outreach_ledger"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The states a letter can be in, as ``models.OutreachState`` spells them. Repeated
#: here rather than imported: a migration describes the schema at its own point in
#: history and must not change when the enum later does.
_STATES = ("entwurf", "raus", "antwort", "absage", "veroeffentlicht")


def upgrade() -> None:
    # batch_alter_table, as every revision here does: SQLite has no full ALTER, and
    # the CHECK that keeps ``state`` a closed set can only be written by rebuilding
    # the table. A plain add_column would take the column and silently drop the
    # constraint, leaving the migrated database looser than the one the models
    # build.
    with op.batch_alter_table("outreach") as batch:
        batch.add_column(sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("released_by", sa.String(length=80), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "state",
                sa.Enum(*_STATES, name="outreach_state", create_constraint=True),
                nullable=False,
                server_default="entwurf",
            )
        )
    # The attribution join reads released letters by their release time, per
    # client, for one reporting period.
    op.create_index("ix_outreach_released_at", "outreach", ["released_at"])


def downgrade() -> None:
    op.drop_index("ix_outreach_released_at", table_name="outreach")
    with op.batch_alter_table("outreach") as batch:
        # Named so it can be dropped here: batch mode rebuilds the table from
        # reflection, and a CHECK that outlives the column it constrains makes the
        # rebuilt table refuse to be created at all.
        batch.drop_constraint("outreach_state", type_="check")
        batch.drop_column("state")
        batch.drop_column("released_by")
        batch.drop_column("released_at")
