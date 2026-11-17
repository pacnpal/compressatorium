"""add_folder_library_layout

A folder Library's layout (#430): where a Platform's Images go. "esde"
(``<root>/<platform>``, ES-DE and Batocera) for every existing Library, the
layout Platform folders were always read in.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-17 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | Sequence[str] | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created column (create_all-then-stamp path), as 0018 does.
    inspector = sa.inspect(op.get_bind())
    if 'folder_libraries' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('folder_libraries')}
    if 'layout' not in existing:
        op.add_column(
            'folder_libraries',
            sa.Column('layout', sa.String(), nullable=False, server_default=sa.text("'esde'")),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
