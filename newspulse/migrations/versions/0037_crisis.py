"""crises: the declared crisis as a row, and the state its tighter cadence reads

One table, and three things in it that are schema guarantees rather than
conventions a caller has to remember.

**One open crisis per mandate.** A partial UNIQUE index over ``client_id`` where
``closed_at IS NULL``. Partial rather than plain, because a mandate may have had
five crises and be in none — a plain unique index would forbid the second one
ever. Two browser tabs pressing "Krise erklären" in the same second therefore
cannot create two rows, and ``newspulse.crisis.declare`` hands the standing row
back rather than letting the caller meet the IntegrityError.

**A closed crisis carries the reason it was closed.** ``closed_at IS NULL OR
close_reason <> ''``. The review of a crisis begins with "why did we stand this
down", and a row that answers it with an empty string is a row nobody can review.

**The level is arithmetic, and the arithmetic is stored beside it.** Four counts
— outlets, articles, negative articles, plus two flags for national reach and
whether the mandate was named — sit next to ``level``. A crisis level a model
estimated is a number nobody can re-derive, and it would be asked about in
exactly the hour it could not be. The CHECK bounds the level to 1..5.

``article_id`` cascades with the article and ``client_id`` with the client. That
is the same posture the rest of this schema takes and it is deliberate here too:
a crisis whose triggering coverage no longer exists cannot be explained, so it
does not outlive it.

Numbering follows the story (UHR-01 was specified as ``0037_crisis``); the chain,
not the number, is what orders a migration. It parents what was at head.

Revision ID: 0037_crisis
Revises: 0036_visibility
Create Date: 2026-08-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_crisis"
down_revision: str | None = "0036_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the same convention
# 0033_market_signals records: a migration has to keep describing the schema it
# created even after the constants in the code have moved on.
_LEVEL_MIN = 1
_LEVEL_MAX = 5

# The condition the open-crisis index is partial on. One spelling, used by the
# index and by nothing else, so the CREATE and the DROP cannot drift.
_OPEN = "closed_at IS NULL"

_OPEN_INDEX = "uq_crises_one_open_per_client"


def upgrade() -> None:
    op.create_table(
        "crises",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("declared_by", sa.String(length=80), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("level", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "outlet_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "article_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "negative_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "national", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("named", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        # The tighter cadence's whole memory. NULL means it has never read, which
        # is due immediately: a crisis declared at nine does not wait an hour for
        # its first reading.
        sa.Column("last_swept_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"level >= {_LEVEL_MIN} AND level <= {_LEVEL_MAX}",
            name="ck_crises_level_range",
        ),
        sa.CheckConstraint(
            f"{_OPEN} OR close_reason <> ''", name="ck_crises_close_reason"
        ),
    )
    op.create_index("ix_crises_client_id", "crises", ["client_id"])
    op.create_index("ix_crises_article_id", "crises", ["article_id"])
    # Both dialect spellings: the predicate is a dialect keyword, and the one a
    # backend does not recognise is dropped rather than refused — which would
    # leave a plain UNIQUE(client_id) here and forbid a mandate a second crisis.
    op.create_index(
        _OPEN_INDEX,
        "crises",
        ["client_id"],
        unique=True,
        sqlite_where=sa.text(_OPEN),
        postgresql_where=sa.text(_OPEN),
    )


def downgrade() -> None:
    op.drop_index(_OPEN_INDEX, table_name="crises")
    op.drop_index("ix_crises_article_id", table_name="crises")
    op.drop_index("ix_crises_client_id", table_name="crises")
    op.drop_table("crises")
