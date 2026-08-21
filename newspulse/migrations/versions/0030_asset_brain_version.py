"""assets.brain_version: every generated text says which standards it was written under

Revision ID: 0030_asset_brain_version
Revises: 0029_guide_check
Create Date: 2026-08-21

The letter and the impulse have carried this since 0023. The six formats arrived
afterwards and did not, which left the artefacts that go out under the client's
own name as the only ones whose provenance the tool could not answer for.

Nullable, and deliberately not backfilled: a row written before there was
anything to stamp did not have a version, and inventing one would put a number
on a text nothing recorded. NULL is the honest answer and the page reads it as
"from before the standards were tracked".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030_asset_brain_version"
down_revision: str | None = "0029_guide_check"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("brain_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "brain_version")
