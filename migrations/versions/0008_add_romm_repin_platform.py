"""add_romm_repin_platform

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: str | Sequence[str] | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The RomM platform the row's source ROM was on.
    #
    # RomM does not hash files on some platforms at all (Switch, Wii U, PS3 and
    # friends -- its NON_HASHABLE_PLATFORMS), so `GET /api/roms/by-hash` can
    # never find the converted file there. Every NSZ, WUX and PS3 ISO re-pin
    # waited out `repin_abandon_days` and was retired, after hashing a
    # multi-GB image on every pass for a digest RomM would never have. Knowing
    # the platform lets the settle pass join on the path instead.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'romm_repin' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('romm_repin')}
    # Nullable, and left null on existing rows: the settle pass backfills them
    # from the source ROM the first time it reaches one.
    if 'platform_id' not in existing:
        op.add_column(
            'romm_repin', sa.Column('platform_id', sa.Integer(), nullable=True),
        )
    if 'platform_slug' not in existing:
        op.add_column(
            'romm_repin', sa.Column('platform_slug', sa.String(), nullable=True),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
