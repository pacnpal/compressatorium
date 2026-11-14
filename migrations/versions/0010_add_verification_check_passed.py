"""add_verification_check_passed

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | Sequence[str] | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Which check a stored verification passed (#298): "intact" (the tool's own
    # container check) or "round_tripped" (the output's content digest equalled
    # the Source's). Every row written before this migration came from a
    # container check, so the server default backfills them as "intact".
    # Tolerate a column already present (create_all-then-stamp path, see 0003).
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("verifications")}
    if "check_passed" in columns:
        return
    op.add_column(
        "verifications",
        sa.Column(
            "check_passed", sa.String(), nullable=False, server_default="intact",
        ),
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project, forward-only.
    raise NotImplementedError("downgrade not supported")
