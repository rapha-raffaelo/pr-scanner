"""guide check: the letter read against the rules the client wrote down

The crosscheck weighs invention and overclaiming, which are judgements about the
world. A No-Go is not a judgement — the client wrote it down — so it is checked in
its own pass and stored in its own three columns rather than folded into
``review``: averaged into one verdict, a written rule ends up diluted into a
remark about tone.

``guide_review`` is JSON rather than the newline-joined text ``review`` uses,
because a breach is a *pair* of quotes: the sentence from the draft and the line
of the guide it collides with. Flattened to one line, the page could no longer say
which half is which, and a pair that cannot be read as a pair is exactly what this
check exists to produce.

Existing letters were never checked, and ``guide_reviewed_by = ''`` is what says
so. ``guide_ok`` defaults to true beside it, the way ``review_ok`` does: it means
"no breach on file", which is only ever read once a model has actually been named.

Numbering: this revision belongs to the guide-check feature and keeps the number
its story gave it. It chains from ``0017_client_facts``, the head of this tree —
the outreach-ledger revisions the numbering leaves room for live in their own
feature and are not here.

Revision ID: 0021_guide_check
Revises: 0017_client_facts
Create Date: 2026-08-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_guide_check"
down_revision: str | None = "0017_client_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches models._EMPTY_JSON_ARRAY: a row that omits the column gets a valid empty
# array rather than NULL, so a letter written before this migration renders as an
# unchecked one instead of raising on the page.
_EMPTY_JSON_ARRAY = "'[]'"


def upgrade() -> None:
    op.add_column(
        "outreach",
        sa.Column(
            "guide_review",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(_EMPTY_JSON_ARRAY),
        ),
    )
    op.add_column(
        "outreach",
        sa.Column(
            "guide_reviewed_by", sa.String(length=80), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "outreach",
        sa.Column(
            "guide_ok", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
    )


def downgrade() -> None:
    op.drop_column("outreach", "guide_ok")
    op.drop_column("outreach", "guide_reviewed_by")
    op.drop_column("outreach", "guide_review")
