"""add_ledger_table

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | Sequence[str] | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path), as 0004 does.
    bind = op.get_bind()
    if 'ledger' in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        'ledger',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.String(), nullable=False),
        sa.Column('finished_at', sa.String(), nullable=False),
        sa.Column('tool', sa.String(), nullable=True),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('source_path', sa.String(), nullable=False),
        sa.Column('output_path', sa.String(), nullable=True),
        sa.Column('input_bytes', sa.Integer(), nullable=True),
        sa.Column('output_bytes', sa.Integer(), nullable=True),
        sa.Column('duration_s', sa.Float(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('check_passed', sa.String(), nullable=False),
        sa.Column('source_deleted', sa.Boolean(), nullable=False),
        sa.Column('deleted_paths', sa.JSON(), nullable=False),
        sa.Column('referrers_updated', sa.JSON(), nullable=False),
        sa.Column('library_id', sa.String(), nullable=True),
        sa.Column('image_key', sa.String(), nullable=True),
        sa.Column('platform', sa.String(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Newest-first listing and age-based pruning both select on finished_at.
    op.create_index('ix_ledger_finished_at', 'ledger', ['finished_at'])
    # A job can be announced terminal twice; this makes the second a no-op.
    op.create_index(
        'ux_ledger_job_finished', 'ledger', ['job_id', 'finished_at'], unique=True,
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
