"""add_folder_libraries_table

Folders registered as Libraries (#303).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | Sequence[str] | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path), as 0004 does.
    bind = op.get_bind()
    if 'folder_libraries' in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        'folder_libraries',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('root', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('schedule_enabled', sa.Boolean(), nullable=False),
        sa.Column('max_per_sweep', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('root'),
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
