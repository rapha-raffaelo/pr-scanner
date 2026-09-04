"""issues, issue_signals, issue_dismissals: the thing that gets three weeks old

Until now the same accusation on Monday and on Friday was two cards on two days.
An issue is the object between the daily card and the declared crisis: one
repeated matter, with an age, a last movement and a growing count of attached
signals. Three tables carry it, and each holds one discipline at the schema so
no future writer has to remember it:

**issues** — the register row. Wahrscheinlichkeit and Wirkung are CHECKed onto
the 1-5 scale *or NULL*, because "noch nicht gesetzt" is a value of its own: the
heatmap shows an ungraded issue in a named column, never at the origin of the
field, and a zero smuggled in as "not set" would put it there. A closed issue
carries its reason — the same one-direction CHECK the crisis has, because "why
did we stop watching this" answered with an empty string is silence three months
later. ``crisis_id`` is SET NULL rather than CASCADE: the issue is the crisis's
prehistory and does not vanish because the crisis row was deleted.

**issue_signals** — one signal on one issue, with the reason it hangs there.
Exactly one of ``article_id``/``signal_id`` (the XOR CHECK), because a signal
that does not resolve to a stored row is not evidence of anything. ``reason``
is CHECKed non-empty: DEC-4 locked "eine unbegründbare Zuordnung wird nicht
gespeichert", and a CHECK survives every future writer of the table, not only
the one that was reviewed. The two UNIQUEs make attaching idempotent — the same
piece hangs on the same issue once, and NULLs do not collide, so the pair stays
out of each other's way.

**issue_dismissals** — one proposal a person waved off. UNIQUE per (client,
article) so a double click, a second tab and a replayed POST all land on the
same dismissal; DEC-3's false alarm costs one click and stays costing one.

Numbering follows the story (RIS-02 was specified as ``0045_issues``); the
chain, not the number, is what orders a migration. It parents what was at head.

Revision ID: 0045_issues
Revises: 0044_reputation_reading
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_issues"
down_revision: str | None = "0044_reputation_reading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported from the models, the same convention
# 0037_crisis and 0044_reputation_reading record: a migration has to keep
# describing the schema it created even after the constants in the code moved on.
_STATUSES = ("offen", "eskaliert", "geschlossen")
_ENUM_NAME = "issue_status"

# The graded scale, and NULL as its own value ("noch nicht gesetzt").
_PROBABILITY = "probability IS NULL OR (probability >= 1 AND probability <= 5)"
_IMPACT = "impact IS NULL OR (impact >= 1 AND impact <= 5)"

# One direction only, like the crisis's own CHECK: it cannot be closed without
# a reason. The other direction is a convention of ``newspulse.issues.close``.
_CLOSE_REASON = "closed_at IS NULL OR close_reason <> ''"

# Exactly one stored row per signal — an article or a market signal, never
# neither and never both.
_ONE_TARGET = "(article_id IS NULL) <> (signal_id IS NULL)"


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("early_indicators", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner", sa.String(80), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.Enum(*_STATUSES, name=_ENUM_NAME, create_constraint=True),
            nullable=False,
            server_default="offen",
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_by", sa.String(80), nullable=False),
        sa.Column("last_moved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=True),
        sa.Column("probability_set_by", sa.String(80), nullable=False, server_default=""),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("impact_set_by", sa.String(80), nullable=False, server_default=""),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("closed_by", sa.String(80), nullable=False, server_default=""),
        sa.Column(
            "crisis_id",
            sa.Integer(),
            sa.ForeignKey("crises.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(_PROBABILITY, name="ck_issues_probability_range"),
        sa.CheckConstraint(_IMPACT, name="ck_issues_impact_range"),
        sa.CheckConstraint(_CLOSE_REASON, name="ck_issues_close_reason"),
    )
    op.create_index("ix_issues_client_id", "issues", ["client_id"])

    op.create_table(
        "issue_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "signal_id",
            sa.Integer(),
            sa.ForeignKey("market_signals.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attached_by", sa.String(80), nullable=False),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_ONE_TARGET, name="ck_issue_signals_one_target"),
        sa.CheckConstraint("reason <> ''", name="ck_issue_signals_reason"),
        sa.UniqueConstraint("issue_id", "article_id", name="uq_issue_signals_article"),
        sa.UniqueConstraint("issue_id", "signal_id", name="uq_issue_signals_signal"),
    )
    op.create_index("ix_issue_signals_issue_id", "issue_signals", ["issue_id"])

    op.create_table(
        "issue_dismissals",
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
        sa.Column("dismissed_by", sa.String(80), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "article_id", name="uq_issue_dismissals_once"),
    )
    op.create_index("ix_issue_dismissals_client_id", "issue_dismissals", ["client_id"])
    op.create_index("ix_issue_dismissals_article_id", "issue_dismissals", ["article_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_dismissals_article_id", table_name="issue_dismissals")
    op.drop_index("ix_issue_dismissals_client_id", table_name="issue_dismissals")
    op.drop_table("issue_dismissals")
    op.drop_index("ix_issue_signals_issue_id", table_name="issue_signals")
    op.drop_table("issue_signals")
    op.drop_index("ix_issues_client_id", table_name="issues")
    op.drop_table("issues")
    # The CHECK the status Enum emitted goes with the ``issues`` table it was
    # on; nothing else holds it, so dropping the tables is the whole teardown —
    # no column is dropped here, so the constraint cannot be orphaned.
