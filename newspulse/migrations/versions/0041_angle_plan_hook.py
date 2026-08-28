"""angles.plan_hook_id: the occasion a plan hook was opened as

Revision ID: 0041_angle_plan_hook
Revises: 0040_plan_hooks
Create Date: 2026-08-28

An impulse comes from the radar and belongs to the morning it was drafted. A
hook is the other way round: a person clicks "Text schreiben" on a dated entry
in the editorial plan, and everything written afterwards belongs to that entry.
Without this column the second click would open a second occasion on the same
date, and the plan page could never say which text came out of which hook.

Nullable and not backfilled — nearly every impulse on file was drafted by the
radar and has no hook, and NULL is exactly that. ``SET NULL`` rather than
``CASCADE``: a recompute that removes an untouched hook must not take a released
press release down with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0041_angle_plan_hook"
down_revision: str | None = "0040_plan_hooks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Batch mode because the column carries a foreign key: SQLite cannot add one
    # to an existing table with a plain ALTER, and the rest of the chain names
    # its constraints for exactly this reason.
    with op.batch_alter_table("angles") as batch:
        batch.add_column(sa.Column("plan_hook_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_angles_plan_hook_id",
            "plan_hooks",
            ["plan_hook_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_angles_plan_hook_id", "angles", ["plan_hook_id"])


def downgrade() -> None:
    op.drop_index("ix_angles_plan_hook_id", table_name="angles")
    with op.batch_alter_table("angles") as batch:
        batch.drop_constraint("fk_angles_plan_hook_id", type_="foreignkey")
        batch.drop_column("plan_hook_id")
