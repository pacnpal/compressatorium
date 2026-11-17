"""add_folder_library_layout

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0024'
down_revision: str | Sequence[str] | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A folder Library's layout (#430): where each Platform's Images go.
    # "esde" for every existing Library, the Platform folders directly under
    # the root, which is how they were always read.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'folder_libraries' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('folder_libraries')}
    if 'layout' not in existing:
        op.add_column(
            'folder_libraries',
            sa.Column('layout', sa.String(), nullable=False, server_default=sa.text("'esde'")),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
