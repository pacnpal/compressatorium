"""add_image_checks

Each Image's last check time and result, for check Rules (#305).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | Sequence[str] | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table (create_all-then-stamp path), as 0012 does.
    if 'image_checks' in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        'image_checks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('library_id', sa.String(), nullable=False),
        sa.Column('image_key', sa.String(), nullable=False),
        sa.Column('checked_at', sa.String(), nullable=False),
        sa.Column('result', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ux_image_checks_library_image', 'image_checks',
        ['library_id', 'image_key'], unique=True,
    )


def downgrade() -> None:
    # The check times go with it; no Image is touched.
    if 'image_checks' in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_index('ux_image_checks_library_image', table_name='image_checks')
        op.drop_table('image_checks')
