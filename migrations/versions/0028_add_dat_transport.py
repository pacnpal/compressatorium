"""add_dat_transport

Each DAT records how it arrived: "https", "http" or "manual" (Codex
4659176015 on #557). A DAT that came over plain HTTP never vouches for a
delete (services.recognized).

Existing rows: a DAT the Redump sync imported (its id is in the
``redump_sync`` preference) can only have come over #557's plain-HTTP
fallback, since redump.org serves no HTTPS, so it is marked "http". Every
other DAT was imported by hand or by the HTTPS-only MAME Redump sync; it is
left NULL, which reads as before.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-22 00:00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | Sequence[str] | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _synced_dat_ids(bind) -> set[str]:
    """The DAT ids the Redump sync's cache names. An unreadable cache names none."""
    if 'preferences' not in sa.inspect(bind).get_table_names():
        return set()
    raw = bind.execute(
        sa.text("SELECT value FROM preferences WHERE key = 'redump_sync'"),
    ).scalar()
    try:
        cache = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return set()
    if not isinstance(cache, dict):
        return set()
    ids: set[str] = set()
    for entry in cache.values():
        if isinstance(entry, dict) and isinstance(entry.get('dat_ids'), list):
            ids.update(i for i in entry['dat_ids'] if isinstance(i, str))
    return ids


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'dats' not in inspector.get_table_names():
        return
    # Tolerate a pre-created column (create_all-then-stamp path), as 0024 does.
    if 'transport' not in {col['name'] for col in inspector.get_columns('dats')}:
        op.add_column('dats', sa.Column('transport', sa.String(), nullable=True))
    synced = sorted(_synced_dat_ids(bind))
    if synced:
        bind.execute(
            sa.text("UPDATE dats SET transport = 'http' "
                    "WHERE transport IS NULL AND id IN :ids")
            .bindparams(sa.bindparam('ids', expanding=True)),
            {'ids': synced},
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
