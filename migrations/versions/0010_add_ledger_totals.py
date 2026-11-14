"""add_ledger_totals

Running totals behind the Prometheus counters; seeded from the Ledger.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | Sequence[str] | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def seed_totals(conn) -> None:
    """Replace the totals with counts and sums taken from the current Ledger."""
    conn.execute(sa.text('DELETE FROM ledger_totals'))
    conn.execute(sa.text(
        'INSERT INTO ledger_totals (status, jobs, input_bytes, output_bytes) '
        'SELECT status, COUNT(*), '
        "COALESCE(SUM(CASE WHEN status = 'completed' THEN input_bytes END), 0), "
        "COALESCE(SUM(CASE WHEN status = 'completed' THEN output_bytes END), 0) "
        'FROM ledger GROUP BY status'
    ))


def upgrade() -> None:
    bind = op.get_bind()
    # Tolerate a pre-created table (create_all-then-stamp path), as 0009 does.
    if 'ledger_totals' in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        'ledger_totals',
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('jobs', sa.Integer(), nullable=False),
        sa.Column('input_bytes', sa.Integer(), nullable=False),
        sa.Column('output_bytes', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('status'),
    )
    seed_totals(bind)


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
