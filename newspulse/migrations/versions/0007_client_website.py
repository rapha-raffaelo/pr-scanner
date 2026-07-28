"""client website, for logo fetching

A logo has to be fetched from somewhere, and guessing a domain from a company
name is unreliable ("H&M" is hm.com, "Otto" is otto.de). Storing the site the
operator already knows makes the fetch deterministic, and gives the card an
obvious place to link to.

Revision ID: 0007_client_website
Revises: 0006_tonality_and_logo
Create Date: 2026-07-28
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_client_website"
down_revision: str | None = "0006_tonality_and_logo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("website", sa.String(512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("website")
