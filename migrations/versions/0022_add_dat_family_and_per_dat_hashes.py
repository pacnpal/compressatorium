"""add_dat_family_and_per_dat_hashes

Keep every DAT's match (#424). ``dat_hashes`` was keyed on (hash,
hash_type), so a hash two DATs share kept only the later import's row; the
key becomes (hash, hash_type, dat_id) and every existing row is kept. Each DAT
also gets a ``family`` (Redump, No-Intro, TOSEC, MAME or other), which the
default DAT ranking orders by; existing DATs are classified from the name and
description they were stored with.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-17 00:00:00
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from services.dat_parser import dat_family

revision: str = '0022'
down_revision: str | Sequence[str] | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    # Tolerate pre-created schema (create_all-then-stamp path), as 0021 does.
    if 'dats' in tables and 'family' not in {c['name'] for c in inspector.get_columns('dats')}:
        with op.batch_alter_table('dats') as batch:
            batch.add_column(sa.Column(
                'family', sa.String(), nullable=False, server_default='other',
            ))
        rows = bind.execute(sa.text('SELECT id, name, description FROM dats')).all()
        for dat_id, name, description in rows:
            family = dat_family(name, description)
            if family != 'other':
                bind.execute(
                    sa.text('UPDATE dats SET family = :f WHERE id = :i'),
                    {'f': family, 'i': dat_id},
                )
    if 'dat_hashes' in tables:
        pk = inspector.get_pk_constraint('dat_hashes').get('constrained_columns') or []
        if 'dat_id' not in pk:
            # SQLite can't alter a primary key in place; batch mode recreates
            # the table and copies every row across. SQLAlchemy warns that
            # the reflected columns' primary_key flags disagree with the new
            # key, which is the change being made.
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', sa.exc.SAWarning)
                with op.batch_alter_table('dat_hashes', recreate='always') as batch:
                    batch.create_primary_key(
                        'pk_dat_hashes', ['hash', 'hash_type', 'dat_id'],
                    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project (forward-only).
    raise NotImplementedError("downgrade not supported")
