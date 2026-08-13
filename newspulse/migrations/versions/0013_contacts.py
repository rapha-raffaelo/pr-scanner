"""contacts: the journalists the consultant actually knows

The pitch list can name who wrote a story, because feeds carry a byline
sometimes. It can never say how to reach them: no feed carries a contact, and a
plausible address derived from a pattern is worse than an empty field, because it
gets used and reaches the wrong person.

So the one place contact details live is a book a human fills in. The pitch list
links each name into it — already there, or a prefilled new entry one edit away.

Keyed on (name, outlet): a journalist who moves masthead is a new contact at the
new one, and the pitch list looks people up by the outlet that published them.

Revision ID: 0013_contacts
Revises: 0012_muted_categories
Create Date: 2026-08-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_contacts"
down_revision: str | None = "0012_muted_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("outlet", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("phone", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("beat", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "outlet", name="uq_contacts_name_outlet"),
    )
    op.create_index("ix_contacts_name", "contacts", ["name"])


def downgrade() -> None:
    op.drop_index("ix_contacts_name", table_name="contacts")
    op.drop_table("contacts")
