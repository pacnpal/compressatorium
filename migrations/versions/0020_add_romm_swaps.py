"""add_romm_swaps

The RomM swap journal (#391): one row per swap that keeps a ROM's id by
renaming its file in RomM, written before each step runs.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-16 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: str | Sequence[str] | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path), as 0012 does.
    if 'romm_swaps' in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        'romm_swaps',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('rom_id', sa.Integer(), nullable=False),
        sa.Column('source_path', sa.String(), nullable=False),
        sa.Column('output_path', sa.String(), nullable=False),
        sa.Column('staged_path', sa.String(), nullable=True),
        sa.Column('parked_path', sa.String(), nullable=True),
        sa.Column('old_name', sa.String(), nullable=False),
        sa.Column('new_name', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_romm_swaps_state', 'romm_swaps', ['state'])


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
