"""add_folder_library_es_frontend

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-16 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: str | Sequence[str] | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A folder Library's "used by an EmulationStation frontend" switch (#386).
    # When on, a conversion rewrites the matching <path> in the Platform
    # folder's gamelist.xml before the Source goes. Off for every existing
    # Library: the app never edits another app's files unless asked.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'folder_libraries' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('folder_libraries')}
    if 'es_frontend' not in existing:
        op.add_column(
            'folder_libraries',
            sa.Column(
                'es_frontend', sa.Boolean(), nullable=False, server_default=sa.text('0'),
            ),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
