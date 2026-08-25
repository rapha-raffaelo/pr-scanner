"""market_signals: the studies, regulatory dates and events a news feed never carries

Deliberately a table of its own rather than a flag on ``articles``. An article is
what a feed syndicated about a company: no body text (Leistungsschutzrecht), it
has already happened, and every query in the tool that touches coverage assumes
that shape. A consultation that closes in five weeks is none of those, and filing
it in ``articles`` would make each of those queries wrong in a way nobody would
notice until a client report counted a consultation as press.

Four date columns, because "when did this happen" is the wrong question for two
of the three classes. ``found_at`` is when the sweep saw it and is never shown;
``published_at`` is the study's actionable date; ``effective_at`` is when a law
lands or a consultation opens, routinely *in the future*; ``deadline_at`` is when
the door closes. Nullable except ``found_at`` — a class that has no such date must
be able to say so rather than carry a fabricated one.

Two unique constraints, both scoped to the client. The URL one is what "unique per
client" means: the same consultation is a real signal for every mandate in the
field. The title one catches an official source that re-issues the same page under
a new URL, which would otherwise arrive as a fresh signal every morning.
``title_hash`` is nullable on purpose — a headline too thin to trust gets no hash,
and NULLs do not collide in a UNIQUE index, so such a row falls back to URL
identity exactly as the article dedup does.

Numbering: the number is the story's (SRC-01 was specified as ``0033_market_signals``)
and the chain, not the number, is what orders a migration — the same convention
``0024_assets`` and ``0029_guide_check`` already record. It parents whatever was at
head, which is ``0030_asset_brain_version``.

Revision ID: 0033_market_signals
Revises: 0032_report_snapshot
Create Date: 2026-08-24
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_market_signals"
down_revision: str | None = "0032_report_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models: a migration has to keep
# describing the schema it created even after the enum in the code has moved on.
_SIGNAL_KINDS = ("studie", "regulierung", "veranstaltung")
_SIGNAL_ORIGINS = ("kuratiert", "suche")


def upgrade() -> None:
    op.create_table(
        "market_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(*_SIGNAL_KINDS, name="signalkind", create_constraint=True),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        # No CHECK bounding this to the past, and that absence is the feature:
        # a regulatory item's whole value is that this date has not arrived yet.
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "origin",
            sa.Enum(*_SIGNAL_ORIGINS, name="signalorigin", create_constraint=True),
            nullable=False,
            server_default="kuratiert",
        ),
        sa.Column("title_hash", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("client_id", "url", name="uq_market_signal_client_url"),
        sa.UniqueConstraint(
            "client_id", "kind", "title_hash", name="uq_market_signal_client_kind_title"
        ),
    )
    op.create_index("ix_market_signals_client_id", "market_signals", ["client_id"])
    op.create_index("ix_market_signals_kind", "market_signals", ["kind"])
    # The market page ranks by what is next rather than by what is newest, so
    # this is the column every read of it orders on.
    op.create_index("ix_market_signals_effective_at", "market_signals", ["effective_at"])


def downgrade() -> None:
    op.drop_index("ix_market_signals_effective_at", table_name="market_signals")
    op.drop_index("ix_market_signals_kind", table_name="market_signals")
    op.drop_index("ix_market_signals_client_id", table_name="market_signals")
    op.drop_table("market_signals")
