"""add_ledger_extra_inputs

The Ledger's list of inputs besides the Source (#428): a patch job's patch
file, each as {"kind", "path", "sha1"}. Rows written before this had none, so
the server default backfills them as an empty list.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-17 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | Sequence[str] | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a column already present (create_all-then-stamp path, see 0003).
    present = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('ledger')}
    if 'extra_inputs' in present:
        return
    with op.batch_alter_table('ledger') as batch:
        batch.add_column(
            sa.Column(
                'extra_inputs', sa.JSON(), nullable=False, server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
