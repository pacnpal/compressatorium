"""add_content_key_file_identity

The file a content key was read from (#389): its device and inode beside its
size and mtime, so a file moved into the copy's place with the same size and
mtime is not taken for the file that was hashed. Null on a row written
before: such a key no longer matches its file, and the next find-duplicates
run reads it again.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-16 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | Sequence[str] | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'content_keys' not in inspector.get_table_names():
        return
    have = {c['name'] for c in inspector.get_columns('content_keys')}
    for column in ('dev', 'ino'):
        if column not in have:
            op.add_column('content_keys', sa.Column(column, sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
