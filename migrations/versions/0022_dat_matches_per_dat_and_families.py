"""dat_matches_per_dat_and_families

The DAT store keeps every DAT's match (#424). ``dat_hashes`` was unique on
``(hash, hash_type)``, so a hash two DATs share kept only the later import;
it becomes unique per DAT, ``(hash, hash_type, dat_id)``, and every existing
row is kept. Each DAT gains a ``family`` (Redump, No-Intro, TOSEC, MAME or
other) and a nullable ``rank`` (the user's order; NULL until they set one).

A DAT imported before this kept only its name and description, so its family
is read from those; a No-Intro set whose header says so only in its homepage
reads "other" until it is imported again.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-17 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: str | Sequence[str] | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same order as services.dat_ranking: a "MAME Redump" set is Redump first.
# Inlined rather than imported so the migration stays what it was when written.
_FAMILY_SQL = """
UPDATE dats SET family = CASE
    WHEN lower(name || ' ' || description) LIKE '%redump%' THEN 'Redump'
    WHEN lower(name || ' ' || description) LIKE '%no-intro%'
      OR lower(name || ' ' || description) LIKE '%no intro%'
      OR lower(name || ' ' || description) LIKE '%nointro%' THEN 'No-Intro'
    WHEN lower(name || ' ' || description) LIKE '%tosec%' THEN 'TOSEC'
    WHEN lower(name || ' ' || description) LIKE 'mame%'
      OR lower(name || ' ' || description) LIKE '% mame%' THEN 'MAME'
    ELSE 'other'
END
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Tolerate pre-created columns (create_all-then-stamp path), as 0021 does.
    present = {c['name'] for c in inspector.get_columns('dats')}
    with op.batch_alter_table('dats') as batch:
        if 'family' not in present:
            batch.add_column(sa.Column(
                'family', sa.String(), nullable=False, server_default='other',
            ))
        if 'rank' not in present:
            batch.add_column(sa.Column('rank', sa.Integer(), nullable=True))
    if 'family' not in present:
        op.execute(_FAMILY_SQL)

    pk = inspector.get_pk_constraint('dat_hashes')['constrained_columns']
    if 'dat_id' in pk:
        return
    # SQLite cannot alter a primary key in place: copy into a table with the
    # new key and swap it in. Every row is unique on the old key, so it is
    # unique on the wider one too.
    op.create_table(
        'dat_hashes_new',
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('hash_type', sa.String(length=8), nullable=False),
        sa.Column('dat_id', sa.String(length=16), nullable=False),
        sa.Column('game_name', sa.String(), nullable=False),
        sa.Column('rom_name', sa.String(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['dat_id'], ['dats.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('hash', 'hash_type', 'dat_id'),
    )
    op.execute(
        'INSERT INTO dat_hashes_new (hash, hash_type, dat_id, game_name, rom_name, size) '
        'SELECT hash, hash_type, dat_id, game_name, rom_name, size FROM dat_hashes',
    )
    op.drop_index('ix_dat_hashes_dat_id', table_name='dat_hashes')
    op.drop_table('dat_hashes')
    op.rename_table('dat_hashes_new', 'dat_hashes')
    op.create_index('ix_dat_hashes_dat_id', 'dat_hashes', ['dat_id'])


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
