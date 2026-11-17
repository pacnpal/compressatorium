"""add_inboxes_table

Folders registered as Inboxes, each feeding one Library (#431).

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
    # Tolerate a pre-created table (create_all-then-stamp path), as 0012 does.
    bind = op.get_bind()
    if 'inboxes' in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        'inboxes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('folder', sa.String(), nullable=False),
        sa.Column('library_id', sa.String(), nullable=False),
        sa.Column('settle_seconds', sa.Integer(), nullable=False),
        sa.Column('processed_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('folder'),
        # A removed Inbox's id is never reused (its Findings are keyed by it).
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
