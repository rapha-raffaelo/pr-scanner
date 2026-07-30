"""comms guide: what a client wants to say, and the documents it came from

Keywords say what is written *about* a mandate. Nothing said what the mandate
wants to say, never says, or in which tone — so every generated text guessed it,
freshly, on every call. The guide is one short text per client because it is
prepended to every prompt; the documents it was distilled from are kept as
extracted text so a later re-run has something to read.

Revision ID: 0010_comms_guide
Revises: 0009_topic_hits
Create Date: 2026-07-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_comms_guide"
down_revision: str | None = "0009_topic_hits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        # NOT NULL with an empty default: every existing client gets an empty
        # guide rather than a NULL every reader would have to special-case.
        batch.add_column(
            sa.Column("comms_guide", sa.Text(), nullable=False, server_default="")
        )
    op.create_table(
        "guide_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("characters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_guide_sources_client_id", "guide_sources", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_guide_sources_client_id", table_name="guide_sources")
    op.drop_table("guide_sources")
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("comms_guide")
