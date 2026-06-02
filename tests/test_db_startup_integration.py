"""End-to-end startup wiring tests.

Exercise the exact sequence ``app/main.py:startup_event`` uses:
``init_engine(create_schema=False)`` → ``apply_migrations()`` →
``init_and_migrate(...)``.  These tests intentionally drive the public
``db`` API, not the FastAPI lifespan, so they're fast and
deterministic.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from services import db as _db

from .conftest import (
    SAMPLE_CHD_METADATA,
    SAMPLE_DAT_STORE,
    SAMPLE_DAT_SYNC,
    SAMPLE_VERIFICATION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_engine():
    _db.engine = None
    _db.SessionLocal = None
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _current_rev(engine) -> str | None:
    from alembic.migration import MigrationContext

    with engine.begin() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _head_rev() -> str:
    """Latest migration revision, read from the script directory.

    Derived rather than hardcoded so adding a migration doesn't require
    touching every revision assertion here.
    """
    from alembic.script import ScriptDirectory

    cfg = _db._alembic_config(_db.make_engine(":memory:"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _run_startup(
    db_path: str,
    data_dir: Path,
    *,
    dat_store: dict | None = None,
    verification: dict | None = None,
    chd_metadata: dict | None = None,
    dat_sync: dict | None = None,
) -> None:
    """Replay the ``startup_event`` ordering against *db_path* and *data_dir*."""
    if dat_store is not None:
        (data_dir / "dat_store.json").write_text(json.dumps(dat_store), encoding="utf-8")
    if verification is not None:
        (data_dir / "verified_chds.json").write_text(
            json.dumps(verification), encoding="utf-8",
        )
    if chd_metadata is not None:
        (data_dir / "chd_metadata.json").write_text(
            json.dumps(chd_metadata), encoding="utf-8",
        )
    if dat_sync is not None:
        (data_dir / "dat_sync.json").write_text(json.dumps(dat_sync), encoding="utf-8")

    _db.init_engine(db_path, create_schema=False)
    _db.apply_migrations()
    _db.init_and_migrate(
        db_path,
        dat_store_json=data_dir / "dat_store.json",
        verification_json=data_dir / "verified_chds.json",
        chd_metadata_json=data_dir / "chd_metadata.json",
        dat_sync_json=data_dir / "dat_sync.json",
    )


# ---------------------------------------------------------------------------
# Happy path: no legacy data
# ---------------------------------------------------------------------------


def test_fresh_disk_no_legacy_json(tmp_path: Path, db_path: str):
    _run_startup(db_path, tmp_path)

    # Alembic drove schema, not create_all.
    assert _current_rev(_db.engine) == _head_rev()

    tables = set(inspect(_db.engine).get_table_names())
    assert _db._BASELINE_TABLES.issubset(tables)
    assert "alembic_version" in tables

    # Nothing to migrate → no .migrated.bak files.
    assert list(tmp_path.glob("*.migrated.bak")) == []

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 0
        assert s.query(_db.Verification).count() == 0
        assert s.query(_db.CHDMetadata).count() == 0
        assert s.query(_db.DATSyncState).count() == 0


# ---------------------------------------------------------------------------
# Happy path: full legacy migration
# ---------------------------------------------------------------------------


def test_full_legacy_migration(tmp_path: Path, db_path: str):
    _run_startup(
        db_path,
        tmp_path,
        dat_store=SAMPLE_DAT_STORE,
        verification=SAMPLE_VERIFICATION,
        chd_metadata=SAMPLE_CHD_METADATA,
        dat_sync=SAMPLE_DAT_SYNC,
    )

    assert _current_rev(_db.engine) == _head_rev()

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 2
        assert s.query(_db.DATHash).count() == 4
        assert s.query(_db.DATMatch).count() == 2
        assert s.query(_db.Verification).count() == 2
        assert s.query(_db.CHDMetadata).count() == 2
        assert s.query(_db.DATSyncState).count() == 1

    for name in ("dat_store.json", "verified_chds.json", "chd_metadata.json", "dat_sync.json"):
        assert not (tmp_path / name).exists(), f"{name} should have been renamed"
        assert (tmp_path / f"{name}.migrated.bak").exists()


# ---------------------------------------------------------------------------
# Pre-Alembic upgrade + partial JSON migration
# ---------------------------------------------------------------------------


def test_pre_alembic_plus_json_coexistence(tmp_path: Path, db_path: str, reset_db_engine):
    # Simulate the pre-Alembic v3.6 install: schema was populated by
    # create_all, and the dats table already has a row (so a new
    # dat_store.json must NOT clobber it).
    eng = _db.init_engine(db_path, create_schema=True)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dats (id, name, description, version, "
                "imported_at, file_count) "
                "VALUES ('pre00001', 'Pre-Alembic', '', '', '', 0)"
            )
        )
    reset_db_engine()

    # Now run the real startup sequence with JSON files dropped into the
    # data dir.  Only the empty tables should be migrated.
    _run_startup(
        db_path,
        tmp_path,
        dat_store=SAMPLE_DAT_STORE,        # skipped, dats non-empty
        verification=SAMPLE_VERIFICATION,  # migrated, empty
        chd_metadata=SAMPLE_CHD_METADATA,  # migrated, empty
        dat_sync=SAMPLE_DAT_SYNC,          # migrated, singleton empty
    )

    # Alembic stamped the baseline and upgraded to head.
    assert _current_rev(_db.engine) == _head_rev()

    with _db.get_session() as s:
        # Pre-existing DAT survived; new dat_store JSON was NOT imported.
        ids = {d.id for d in s.query(_db.DAT).all()}
        assert ids == {"pre00001"}
        # Other stores migrated cleanly.
        assert s.query(_db.Verification).count() == 2
        assert s.query(_db.CHDMetadata).count() == 2
        assert s.query(_db.DATSyncState).count() == 1

    # I2: dat_store.json preserved (needs manual resolution); others
    # renamed to .migrated.bak.
    assert (tmp_path / "dat_store.json").exists()
    assert not (tmp_path / "dat_store.json.migrated.bak").exists()
    assert (tmp_path / "verified_chds.json.migrated.bak").exists()
    assert (tmp_path / "chd_metadata.json.migrated.bak").exists()
    assert (tmp_path / "dat_sync.json.migrated.bak").exists()


# ---------------------------------------------------------------------------
# Startup idempotency
# ---------------------------------------------------------------------------


def test_startup_is_idempotent_across_restarts(
    tmp_path: Path, db_path: str, reset_db_engine,
):
    _run_startup(
        db_path,
        tmp_path,
        dat_store=SAMPLE_DAT_STORE,
        verification=SAMPLE_VERIFICATION,
    )

    with _db.get_session() as s:
        first_dats = s.query(_db.DAT).count()
        first_verifs = s.query(_db.Verification).count()
        first_rev = _current_rev(_db.engine)

    # Simulate a second process start.
    reset_db_engine()
    _run_startup(db_path, tmp_path)  # no JSON files left, nothing to migrate

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == first_dats
        assert s.query(_db.Verification).count() == first_verifs
        assert _current_rev(_db.engine) == first_rev
