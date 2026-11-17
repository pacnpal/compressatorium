"""add_romm_swap_recovery_fields

What startup recovery of an unfinished RomM swap (#392) needs from its
journal row: the RomM instance the swap ran against (its URL and library
root), and the fingerprints that tell the Source (size, mtime) and the
output (size, SHA-1) apart wherever they are. Rows written before this have
none of them; recovery treats such a row as one it can't tell about.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-17 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: str | Sequence[str] | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ('romm_url', sa.String()),
    ('library_root', sa.String()),
    ('source_size', sa.BigInteger()),
    ('source_mtime_ns', sa.BigInteger()),
    ('output_size', sa.BigInteger()),
    ('output_sha1', sa.String()),
)


def upgrade() -> None:
    # Tolerate pre-created columns (create_all-then-stamp path), as 0020 does
    # its table.
    present = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('romm_swaps')}
    with op.batch_alter_table('romm_swaps') as batch:
        for name, kind in _COLUMNS:
            if name not in present:
                batch.add_column(sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
