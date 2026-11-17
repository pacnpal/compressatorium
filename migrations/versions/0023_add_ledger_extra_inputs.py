"""add_ledger_extra_inputs

The inputs a job read besides its Source (#428): a patch job's patch file,
each as ``{"path": ..., "sha1": ...}``. Rows written before this had none,
so they backfill as an empty list.

Revision ID: 0023
Revises: 0021
Create Date: 2026-11-17 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | Sequence[str] | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a column already present (create_all-then-stamp path, see 0010).
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('ledger')}
    if 'extra_inputs' in columns:
        return
    with op.batch_alter_table('ledger') as batch:
        batch.add_column(
            sa.Column('extra_inputs', sa.JSON(), nullable=False, server_default='[]'),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
