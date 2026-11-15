"""add_platform_mappings_and_findings

The user's folder-to-Platform mappings, and the Findings store (#304).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | Sequence[str] | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate pre-created tables (create_all-then-stamp path), as 0004 does.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'platform_mappings' not in tables:
        op.create_table(
            'platform_mappings',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('library_id', sa.String(), nullable=False),
            sa.Column('folder', sa.String(), nullable=False),
            sa.Column('platform', sa.String(), nullable=False),
            sa.Column('updated_at', sa.String(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ux_platform_mappings_library_folder', 'platform_mappings',
            ['library_id', 'folder'], unique=True,
        )
    if 'findings' not in tables:
        op.create_table(
            'findings',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('library_id', sa.String(), nullable=False),
            sa.Column('image_key', sa.String(), nullable=False),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('path', sa.String(), nullable=False),
            sa.Column('reason', sa.String(), nullable=False),
            sa.Column('cause', sa.JSON(), nullable=False),
            sa.Column('fingerprint', sa.String(), nullable=False),
            sa.Column('dismissed_fingerprint', sa.String(), nullable=True),
            sa.Column('first_seen', sa.String(), nullable=False),
            sa.Column('last_seen', sa.String(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(
            'ux_findings_library_image_kind', 'findings',
            ['library_id', 'image_key', 'kind'], unique=True,
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project: forward-only.
    raise NotImplementedError("downgrade not supported")
