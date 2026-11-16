"""add_content_keys

The duplicate finder's content key per file, tied to its size and mtime, and
each Library's last find-duplicates run (#388).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-16 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | Sequence[str] | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path), as 0012 does.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'content_keys' not in existing:
        op.create_table(
            'content_keys',
            sa.Column('path', sa.String(), nullable=False),
            sa.Column('size', sa.BigInteger(), nullable=False),
            sa.Column('mtime_ns', sa.BigInteger(), nullable=False),
            sa.Column('key', sa.String(), nullable=True),
            sa.Column('container', sa.String(), nullable=False),
            sa.Column('computed_at', sa.String(), nullable=False),
            sa.PrimaryKeyConstraint('path'),
        )
        op.create_index('ix_content_keys_key', 'content_keys', ['key'])
    if 'duplicate_runs' not in existing:
        op.create_table(
            'duplicate_runs',
            sa.Column('library_id', sa.String(), nullable=False),
            sa.Column('finished_at', sa.String(), nullable=False),
            sa.Column('summary', sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint('library_id'),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
