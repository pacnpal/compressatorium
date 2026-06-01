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
    # Tolerate a pre-created table. apply_migrations() documents the
    # create_all-then-stamp path as supported: a DB built from the current
    # ORM metadata (which already includes the Preference model) gets
    # stamped at baseline 0001, then this migration runs. An unconditional
    # CREATE TABLE would raise "table preferences already exists" there, so
    # only create it when missing.
    bind = op.get_bind()
    if 'preferences' in sa.inspect(bind).get_table_names():
        return
    op.create_table('preferences',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
