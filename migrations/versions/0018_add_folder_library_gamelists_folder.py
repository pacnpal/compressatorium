"""add_folder_library_gamelists_folder

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-16 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: str | Sequence[str] | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A folder Library's ES-DE gamelists folder (#387): where ES-DE keeps one
    # gamelist per system. Null (none) for every existing Library, which keeps
    # the Platform folder lookup.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'folder_libraries' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('folder_libraries')}
    if 'gamelists_folder' not in existing:
        op.add_column(
            'folder_libraries', sa.Column('gamelists_folder', sa.String(), nullable=True),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
