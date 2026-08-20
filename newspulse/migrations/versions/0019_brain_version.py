"""brain_version: every generated text says which standards it was written under

The ledger records the human act; this records the machine one. A stored angle,
letter or brief carries the portfolio-wide brain version that was in force when
its prompt was composed, so a letter from March can be read against what the
house believed in March rather than against what it believes today.

Nullable, and deliberately without a server default. Rows that already exist were
written before anything was recorded, and ``0`` is not a neutral filler here: zero
is a true statement — the standards have never been changed on this install — so
backfilling it would make every old row claim standards nobody ever wrote down.
NULL is the honest answer and the interface renders it as "unbekannt".

Revision ID: 0019_brain_version
Revises: 0018_brain_overrides
Create Date: 2026-08-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_brain_version"
down_revision: str | None = "0018_brain_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table holding a text a generator wrote. All three at once and not just
#: the two with a page: a stamp that is on the convenient generators only is one
#: whose absence says nothing.
_STAMPED_TABLES = ("angles", "outreach", "advisories")

#: The cross-check is a second model call under a second brain prompt, composed
#: seconds after the letter it reads. Almost always the same version, and
#: "almost" is the problem: an edit landing between the two calls would file the
#: checker's text under a version it never read. Its own column, so the row does
#: not have to make one claim about two texts.
_SECOND_TEXT_ON_A_LETTER = ("outreach", "review_brain_version")


def upgrade() -> None:
    for table in _STAMPED_TABLES:
        op.add_column(table, sa.Column("brain_version", sa.Integer(), nullable=True))
    table, column = _SECOND_TEXT_ON_A_LETTER
    op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    table, column = _SECOND_TEXT_ON_A_LETTER
    op.drop_column(table, column)
    for table in _STAMPED_TABLES:
        op.drop_column(table, "brain_version")
