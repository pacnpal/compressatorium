"""Verify SQLite PRAGMAs are actually applied to every connection.

WAL, ``foreign_keys=ON``, and ``busy_timeout=30000`` are load-bearing
for the app's concurrency and referential-integrity guarantees.  These
tests don't just read the configured value — they prove the PRAGMA is
in force on *every* checked-out connection (SQLite PRAGMAs are
per-connection) and that FK enforcement actually rejects invalid
inserts.
"""

# ruff: noqa: S101

from __future__ import annotations


import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services import db as _db


@pytest.fixture(autouse=True)
def _reset_engine():
    _db.engine = None
    _db.SessionLocal = None
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _pragma(engine, name: str):
    with engine.begin() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_journal_mode_is_wal(db_path: str):
    eng = _db.init_engine(db_path, create_schema=False)
    assert _pragma(eng, "journal_mode").lower() == "wal"


def test_synchronous_is_normal(db_path: str):
    eng = _db.init_engine(db_path, create_schema=False)
    # NORMAL == 1.  FULL == 2.  OFF == 0.
    assert _pragma(eng, "synchronous") == 1


def test_busy_timeout_is_at_least_30_seconds(db_path: str):
    eng = _db.init_engine(db_path, create_schema=False)
    assert _pragma(eng, "busy_timeout") >= 30000


def test_foreign_keys_enabled_on_every_connection(db_path: str):
    """PRAGMA foreign_keys is per-connection; with NullPool every
    checkout gets a fresh connection.  Confirm the pragma hook fires
    on each."""
    eng = _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()

    # Open two independent connections back-to-back.
    with eng.connect() as c1:
        assert c1.execute(text("PRAGMA foreign_keys")).scalar() == 1
    with eng.connect() as c2:
        assert c2.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_foreign_key_enforcement_rejects_orphaned_hash(db_path: str):
    """With ``foreign_keys=ON``, inserting a DATHash that references a
    non-existent ``dat_id`` must raise IntegrityError.  If FKs were
    silently off, the row would land and CASCADE semantics would
    break."""
    _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()

    with _db.get_session() as s:
        s.add(_db.DATHash(
            hash="a" * 40,
            hash_type="sha1",
            dat_id="nonexistent",
            game_name="x",
            rom_name="x.iso",
            size=1,
        ))
        with pytest.raises(IntegrityError):
            s.commit()
