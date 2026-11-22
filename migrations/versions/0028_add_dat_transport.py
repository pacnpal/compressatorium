"""add_dat_transport

Each DAT records how it arrived: "https", "http" or "manual" (#557's
Codex security review). A DAT that came over plain HTTP never vouches for a
delete (services.recognized).

Existing rows fail closed: no existing row can prove it
came over HTTPS, and the ``redump_sync`` cache is not a complete history (a
partly failed sync imports DATs it never records). A row is marked "http"
when it could have come over #557's plain-HTTP fallback:

* its family is Redump (its header names Redump or redump.org), whoever
  imported it: a Redump DAT imported by hand loses its delete proof until it
  is imported again;
* it was imported on or after :data:`FALLBACK_POSSIBLE_FROM` (or its import
  time can't be read), whatever its header says: a DAT swapped on the way
  could claim any name;
* the sync cache names it.

Only an older, non-Redump DAT is left NULL, which reads as before: it was
imported by hand or by the HTTPS-only MAME Redump sync before the fallback
existed.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-22 00:00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | Sequence[str] | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A day before #557's fallback was first written (2026-11-22T15:23Z), so a
# clock that runs a little slow can't place a fallback import before it.
FALLBACK_POSSIBLE_FROM = datetime(2026, 11, 21, tzinfo=timezone.utc)


def _could_be_plain_http(imported_at: str | None) -> bool:
    """Whether a DAT imported at *imported_at* could have come over the
    fallback: on or after :data:`FALLBACK_POSSIBLE_FROM`, or unreadable."""
    try:
        when = datetime.fromisoformat((imported_at or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= FALLBACK_POSSIBLE_FROM


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
    synced = _synced_dat_ids(bind)
    rows = bind.execute(
        sa.text("SELECT id, family, imported_at FROM dats WHERE transport IS NULL"),
    ).all()
    suspect = sorted(
        dat_id for dat_id, family, imported_at in rows
        if family == 'Redump' or dat_id in synced or _could_be_plain_http(imported_at)
    )
    if suspect:
        bind.execute(
            sa.text("UPDATE dats SET transport = 'http' WHERE id IN :ids")
            .bindparams(sa.bindparam('ids', expanding=True)),
            {'ids': suspect},
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
