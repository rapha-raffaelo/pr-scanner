"""two narrowings of the name match, for a company named after something else

The matcher pairs an article with a company when the company's name occurs. For
most of a portfolio that is the whole job. For a company whose name is also a
word it is not: the yardstick "G-20" collected 25 articles in a month about the
summit — NATO, China, heads of government — and every one of them cost a paid
analyzer call to conclude "no".

Two columns because they answer two different questions:

``excluded_terms`` — if one occurs, this is not the company. Surgical: "Gipfel"
cuts the summit and leaves the firm standing.
``required_terms`` — if none occurs, this is not the company. The blunt one, for
a name that is a common word; G-20's own is "Liquidity".

Both additive, both empty for every existing row, and empty means "no
narrowing" — so nothing changes for the companies whose name is their own.

Revision ID: 0055_client_match_terms
Revises: 0054_newsjack_outlook
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_client_match_terms"
down_revision: str | None = "0054_newsjack_outlook"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        for column in ("excluded_terms", "required_terms"):
            batch.add_column(
                sa.Column(
                    column,
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        for column in ("required_terms", "excluded_terms"):
            batch.drop_column(column)
