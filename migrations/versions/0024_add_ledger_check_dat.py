"""add_ledger_check_dat

The DAT that Recognized a job's output (#423): the Recognized delete proof
lets a Source go only when the output's SHA-1 is in a DAT or Hasheous, and
the Ledger records which. None on every row written before it.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-17 00:00:00
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
    # Tolerate a pre-created column (create_all-then-stamp path), as 0023 does.
    inspector = sa.inspect(op.get_bind())
    if 'ledger' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('ledger')}
    if 'check_dat' not in existing:
        op.add_column('ledger', sa.Column('check_dat', sa.String(), nullable=True))


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
