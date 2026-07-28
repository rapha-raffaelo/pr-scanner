"""tonality per analysis, logo per client

Tonality is the question every client actually asks — "how did we come across?" —
and the archive could not answer it. It is judged from the *client's*
perspective, not the article's: a neutrally written report about job losses is
negative for the company it is about.

Existing analyses become ``unbekannt`` rather than ``neutral``. They were written
before the field existed, and recording an honest gap is better than inventing a
verdict for several thousand rows.

``clients.logo_url`` is optional; the dashboard falls back to a generated
monogram, so an unconfigured portfolio still looks finished.

Revision ID: 0006_tonality_and_logo
Revises: 0005_client_competitors
Create Date: 2026-07-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_tonality_and_logo"
down_revision: str | None = "0005_client_competitors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TONALITIES = ("positiv", "neutral", "negativ", "unbekannt")


def upgrade() -> None:
    with op.batch_alter_table("analyses") as batch:
        batch.add_column(
            sa.Column(
                "tonality",
                sa.Enum(*_TONALITIES, name="tonality", create_constraint=True),
                nullable=False,
                server_default="unbekannt",
            )
        )
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("logo_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("logo_url")
    with op.batch_alter_table("analyses") as batch:
        batch.drop_column("tonality")
