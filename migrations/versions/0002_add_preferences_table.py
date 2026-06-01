"""add_preferences_table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-31 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('preferences',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
