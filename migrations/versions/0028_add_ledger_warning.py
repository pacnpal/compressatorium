"""add_ledger_warning

What a job warned about as it ended, whatever its status (#544): an eXoDOS
repack whose config names files outside the game, the reason a watch folder's
archive is waiting for room. Rows written before this warned of nothing, so
the column is nullable and they read as None.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-22 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | Sequence[str] | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a column already present (create_all-then-stamp path, see 0003).
    present = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('ledger')}
    if 'warning' in present:
        return
    with op.batch_alter_table('ledger') as batch:
        batch.add_column(sa.Column('warning', sa.String(), nullable=True))


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
