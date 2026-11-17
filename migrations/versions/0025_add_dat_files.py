"""add_dat_files

Every file each imported DAT lists for each game (#426): the manifest an
Image rename names a sheet's tracks after. Kept apart from ``dat_hashes``,
which keeps one row per hash and DAT, so a track whose bytes another file of
the DAT shares would be missing from it. Empty after the upgrade: a DAT
already imported gets its rows when it is imported again (until then the
rename reads the hash index, and a sheet whose tracks don't all pair is
skipped).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-17 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0025'
down_revision: str | Sequence[str] | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path).
    if 'dat_files' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'dat_files',
        sa.Column('dat_id', sa.String(length=16), nullable=False),
        sa.Column('game_name', sa.String(), nullable=False),
        sa.Column('rom_name', sa.String(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['dat_id'], ['dats.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('dat_id', 'game_name', 'rom_name'),
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
