"""index articles.published_at

Every day-scoped read filters on ``articles.published_at``: the Today view's
local-day window, the client archive's date-range filter, and the Mandanten
overview's per-client counts. Without an index each of those full-scans
``articles``, which is invisible at a hundred rows and costly once the archive
grows to the thousands-across-years the tool is designed to hold (DEC-3 chose a
server-rendered app precisely so those reads stay cheap as history grows).

Revision ID: 0003_index_published_at
Revises: 0002_add_is_relevant
Create Date: 2026-07-27
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_index_published_at"
down_revision: str | None = "0002_add_is_relevant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_articles_published_at", "articles", ["published_at"])


def downgrade() -> None:
    op.drop_index("ix_articles_published_at", table_name="articles")
