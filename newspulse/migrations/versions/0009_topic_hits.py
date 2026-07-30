"""topic_hits: which market article the radar surfaced for which client

The radar's articles were stored with nothing linking them to the client whose
themes found them — the pairing lived only in memory for the length of a run. So
the market material was in the database and attached to nobody: not browsable, and
useless for ranking which outlets cover a client's subject.

Deliberately a second table rather than a flag on ``analyses``. An analysis says
"this article is about this client"; a topic hit says "this article is about what
the client does, and never names them". Merging the two would put market stories
into a mandate's own coverage count.

Revision ID: 0009_topic_hits
Revises: 0008_angles
Create Date: 2026-07-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_topic_hits"
down_revision: str | None = "0008_angles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_hits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        # A re-run that re-surfaces the same story must not stack duplicates, the
        # same posture the analyses table takes.
        sa.UniqueConstraint("article_id", "client_id", name="uq_topic_hit_article_client"),
    )
    op.create_index("ix_topic_hits_article_id", "topic_hits", ["article_id"])
    op.create_index("ix_topic_hits_client_id", "topic_hits", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_topic_hits_client_id", table_name="topic_hits")
    op.drop_index("ix_topic_hits_article_id", table_name="topic_hits")
    op.drop_table("topic_hits")
