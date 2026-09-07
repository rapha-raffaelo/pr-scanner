"""the contact book keeps the given name and the surname apart

"hier sollten wir vor und nachnamen trennen"

The book had one ``name``, so the form asked for one thing and a consultant
typing a journalist into it produced "Kröner" — a surname in a field the whole
tool reads as a full name. A salutation cannot be built from that without
guessing, and a roster cannot sort by surname at all.

``name`` stays and stays authoritative: a byline arrives from a feed as one
string and ``contacts.find`` matches against exactly that, so splitting it into
two columns and dropping it would break every link between a letter and the
person it went to. The parts sit beside it and the form composes it.

Both nullable-as-empty rather than backfilled by a script: splitting "Anna
Maria von der Leyen" is a guess, and a guess written into the database looks
like a fact afterwards. The guess is made in the form, where a person sees it
and can correct it before it is saved.

Revision ID: 0052_contact_name_parts
Revises: 0051_angle_dismissed
Create Date: 2026-09-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_contact_name_parts"
down_revision: str | None = "0051_angle_dismissed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(
            sa.Column("first_name", sa.String(length=100), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("last_name", sa.String(length=100), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("last_name")
        batch.drop_column("first_name")
