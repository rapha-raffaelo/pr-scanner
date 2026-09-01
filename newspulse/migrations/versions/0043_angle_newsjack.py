"""angles.newsjack_id: the occasion a fast-lane opportunity was opened as

Revision ID: 0043_angle_newsjack
Revises: 0039_newsjack, 0042_crisis_dismissals
Create Date: 2026-08-31

The same shape as ``0041_angle_plan_hook``, for the same reason one clock over:
a person clicks "Text schreiben" on the fast lane's card (UHR-05), and every
text written afterwards belongs to that opportunity. Without this column a
second click would open a second occasion beside the first, and neither the
card nor the mandate's archive could say which text came out of which window.

Nullable and not backfilled — nearly every impulse on file was drafted by the
radar or opened from a plan hook, and NULL is exactly that. ``SET NULL`` rather
than ``CASCADE``: deleting a weighed story must not take a released text down
with it.

The index over it is unique and partial, like the plan hook's: unique because
the read-then-insert in ``assets_view.occasion_for_opportunity`` runs in
FastAPI's threadpool and a double click is two requests that both find nothing;
partial because NULL is the normal value and a plain unique index would allow
one ordinary impulse in the entire table.

This revision also merges the chain's two heads. ``0039_newsjack`` and
``0042_crisis_dismissals`` were written on sibling branches off
``0038_crisis_assets`` and landed side by side, which left ``upgrade head``
with two answers; naming both as parents is the same repair ``0038`` made for
the previous fork.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0043_angle_newsjack"
down_revision: tuple[str, ...] | str | None = (
    "0039_newsjack",
    "0042_crisis_dismissals",
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Batch mode because the column carries a foreign key: SQLite cannot add
    # one to an existing table with a plain ALTER.
    with op.batch_alter_table("angles") as batch:
        batch.add_column(sa.Column("newsjack_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_angles_newsjack_id",
            "newsjack_opportunities",
            ["newsjack_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ux_angles_newsjack",
        "angles",
        ["newsjack_id"],
        unique=True,
        sqlite_where=sa.text("newsjack_id IS NOT NULL"),
        postgresql_where=sa.text("newsjack_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_angles_newsjack", table_name="angles")
    with op.batch_alter_table("angles") as batch:
        batch.drop_constraint("fk_angles_newsjack_id", type_="foreignkey")
        batch.drop_column("newsjack_id")
