"""add_romm_repin_plan_generation

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-15 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: str | Sequence[str] | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The generation of the re-pin plan that wrote each row (#337).
    #
    # A plan checks its destinations, awaits several probes, then records. A
    # slow or abandoned plan could resume after a newer run had recorded the
    # same destination and supersede that run's row, leaving its queued
    # conversion with no metadata. The generation is taken when the plan
    # starts, and `record()` never supersedes a row with a higher one.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'romm_repin' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('romm_repin')}
    # Nullable, and left null on existing rows: NULL reads as the oldest
    # generation, so any new plan may still supersede them.
    if 'plan_generation' not in existing:
        op.add_column(
            'romm_repin', sa.Column('plan_generation', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
