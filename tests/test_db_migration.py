"""Tests for the JSON → SQLite migration.

Covers the five no-data-loss invariants from the migration plan:

* **I1** — Successful migration renames JSON to ``.migrated.bak``;
  the original is gone.
* **I2** — Failed migration leaves the JSON source *untouched*.
* **I3** — Row counts post-migration exactly equal JSON entry counts
  (with payload round-trip spot-checks).
* **I4** — Re-running migration is a no-op; partial installs finish.
* **I5** — Corrupt JSON is renamed to ``.corrupt`` and does not block
  other stores.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from services import db


# ---------------------------------------------------------------------------
# Fixtures + sample JSON blobs
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh SQLite file per test (not :memory: because we want true
    persistence behaviour — the migration writes to disk)."""
    return str(tmp_path / "compressatorium.db")


@pytest.fixture(autouse=True)
def _reset_engine():
    """Ensure each test gets a fresh module-level engine."""
    db.engine = None
    db.SessionLocal = None
    yield
    if db.engine is not None:
        db.engine.dispose()
    db.engine = None
    db.SessionLocal = None


SAMPLE_DAT_STORE = {
    "dats": {
        "abc12345": {
            "id": "abc12345",
            "name": "Nintendo - GameCube",
            "description": "Redump GC",
            "version": "0.285",
            "imported_at": "2026-04-11T00:00:00Z",
            "file_count": 2,
        },
        "def67890": {
            "id": "def67890",
            "name": "Nintendo - Wii",
            "description": "Redump Wii",
            "version": "0.285",
            "imported_at": "2026-04-11T00:00:00Z",
            "file_count": 1,
        },
    },
    "hashes": {
        "sha1": {
            "a" * 40: {
                "dat_id": "abc12345",
                "game_name": "Super Mario Sunshine",
                "rom_name": "Super Mario Sunshine.iso",
                "size": 1459978240,
            },
            "b" * 40: {
                "dat_id": "abc12345",
                "game_name": "Metroid Prime",
                "rom_name": "Metroid Prime.iso",
                "size": 1459978240,
            },
            "c" * 40: {
                "dat_id": "def67890",
                "game_name": "Wii Sports",
                "rom_name": "Wii Sports.iso",
                "size": 4699979776,
            },
        },
        "md5": {
            "a" * 32: {
                "dat_id": "abc12345",
                "game_name": "Super Mario Sunshine",
                "rom_name": "Super Mario Sunshine.iso",
                "size": 1459978240,
            },
        },
    },
    "matches": {
        "/data/gc/mario.chd": {
            "path": "/data/gc/mario.chd",
            "matched": True,
            "dat_id": "abc12345",
            "dat_name": "Nintendo - GameCube",
            "game_name": "Super Mario Sunshine",
            "rom_name": "Super Mario Sunshine.iso",
            "match_type": "file_sha1",
            "file_hash": "a" * 40,
        },
        "/data/misc/unknown.chd": {
            "path": "/data/misc/unknown.chd",
            "matched": False,
        },
    },
}

SAMPLE_VERIFICATION = {
    "/data/gc/mario.chd": {
        "chd_path": "/data/gc/mario.chd",
        "source_path": "/data/gc/mario.iso",
        "verified_at": "2026-04-10T12:00:00Z",
    },
    "/data/wii/sports.chd": {
        "chd_path": "/data/wii/sports.chd",
        "source_path": None,
        "verified_at": "2026-04-10T13:00:00Z",
    },
}

SAMPLE_CHD_METADATA = {
    "/data/gc/mario.chd": {
        "chd_path": "/data/gc/mario.chd",
        "metadata": {"file": "mario.chd", "sha1": "a" * 40, "raw_data": "..."},
        "media_type": "dvd",
        "mtime": 1700000000.0,
        "cached_at": "2026-04-10T14:00:00Z",
        "disc_id_checked": True,
        "disc_id_checked_mtime": 1700000000.0,
        "game_id": "GMSE01",
        "title": "Super Mario Sunshine",
    },
    "/data/cd/psx.chd": {
        "chd_path": "/data/cd/psx.chd",
        "metadata": {"file": "psx.chd", "sha1": "b" * 40},
        "media_type": "cd",
        "mtime": 1700001000.0,
        "cached_at": "2026-04-10T14:05:00Z",
        "disc_id_checked": False,
        "disc_id_checked_mtime": None,
        "game_id": None,
        "title": None,
    },
}

SAMPLE_DAT_SYNC = {
    "last_sync_tag": "0.285",
    "last_sync_at": "2026-04-11T00:00:00Z",
    "last_sync_files": 69,
}


# ---------------------------------------------------------------------------
# Happy-path (I1 + I3)
# ---------------------------------------------------------------------------


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_successful_migration_renames_json_and_populates_db(
    tmp_path: Path, db_path: str,
):
    dat_json = _write(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    ver_json = _write(tmp_path / "verified_chds.json", SAMPLE_VERIFICATION)
    meta_json = _write(tmp_path / "chd_metadata.json", SAMPLE_CHD_METADATA)
    sync_json = _write(tmp_path / "dat_sync.json", SAMPLE_DAT_SYNC)

    db.init_and_migrate(
        db_path,
        dat_store_json=dat_json,
        verification_json=ver_json,
        chd_metadata_json=meta_json,
        dat_sync_json=sync_json,
    )

    # I1: originals gone, .migrated.bak exist.
    for p in (dat_json, ver_json, meta_json, sync_json):
        assert not p.exists(), f"{p} should have been renamed"
        assert Path(str(p) + ".migrated.bak").exists(), f"{p}.migrated.bak missing"

    # I3: counts match JSON.
    with db.get_session() as s:
        assert s.query(db.DAT).count() == 2
        # 3 sha1 + 1 md5 = 4 hash rows.
        assert s.query(db.DATHash).count() == 4
        assert s.query(db.DATMatch).count() == 2
        assert s.query(db.Verification).count() == 2
        assert s.query(db.CHDMetadata).count() == 2
        assert s.query(db.DATSyncState).count() == 1

        # Round-trip spot check: match payload fully preserved.
        match = s.get(db.DATMatch, "/data/gc/mario.chd")
        assert match is not None
        assert match.matched is True
        assert match.dat_id == "abc12345"
        assert match.payload["file_hash"] == "a" * 40

        # Singleton sync row.
        sync_row = s.get(db.DATSyncState, 1)
        assert sync_row.last_sync_tag == "0.285"
        assert sync_row.last_sync_files == 69

        # CHD metadata JSON column preserved exactly.
        meta_row = s.get(db.CHDMetadata, "/data/gc/mario.chd")
        assert meta_row.metadata_json == {
            "file": "mario.chd", "sha1": "a" * 40, "raw_data": "...",
        }
        assert meta_row.disc_id_checked is True
        assert meta_row.game_id == "GMSE01"

        # Hash lookup works.
        row = s.scalars(
            select(db.DATHash).where(
                db.DATHash.hash == "a" * 40, db.DATHash.hash_type == "sha1",
            )
        ).one_or_none()
        assert row is not None
        assert row.dat_id == "abc12345"


# ---------------------------------------------------------------------------
# I4: idempotency and partial re-run
# ---------------------------------------------------------------------------


def test_migration_is_idempotent_on_second_run(tmp_path: Path, db_path: str):
    dat_json = _write(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    db.init_and_migrate(db_path, dat_store_json=dat_json)

    # First run: JSON renamed, tables populated.
    assert not dat_json.exists()
    assert (tmp_path / "dat_store.json.migrated.bak").exists()
    with db.get_session() as s:
        dat_count_first = s.query(db.DAT).count()
        match_count_first = s.query(db.DATMatch).count()

    # Reset module state and rerun with the same (now-backup) JSON path.
    db.engine.dispose()
    db.engine = None
    db.SessionLocal = None

    db.init_and_migrate(db_path, dat_store_json=dat_json)  # original path; doesn't exist anymore

    # Tables unchanged, no duplicates.
    with db.get_session() as s:
        assert s.query(db.DAT).count() == dat_count_first
        assert s.query(db.DATMatch).count() == match_count_first


def test_partial_migration_finishes_missing_stores_on_rerun(
    tmp_path: Path, db_path: str,
):
    # First run: only dat_store migrates (verifications JSON absent).
    dat_json = _write(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    db.init_and_migrate(db_path, dat_store_json=dat_json)

    with db.get_session() as s:
        assert s.query(db.DAT).count() == 2
        assert s.query(db.Verification).count() == 0

    # Simulate a second startup: dat_store is already migrated, but
    # verified_chds.json has now been placed in the directory.
    db.engine.dispose()
    db.engine = None
    db.SessionLocal = None
    ver_json = _write(tmp_path / "verified_chds.json", SAMPLE_VERIFICATION)

    db.init_and_migrate(
        db_path,
        dat_store_json=dat_json,          # gone now — skipped
        verification_json=ver_json,       # fresh — imported
    )

    with db.get_session() as s:
        assert s.query(db.DAT).count() == 2           # unchanged
        assert s.query(db.Verification).count() == 2  # newly migrated
    assert not ver_json.exists()
    assert (tmp_path / "verified_chds.json.migrated.bak").exists()


def test_nonempty_db_table_skips_import_and_preserves_json(
    tmp_path: Path, db_path: str,
):
    """If DB already has rows in the target table, don't re-import.  The
    user may have manually populated the DB; we should never clobber
    it.  JSON must be preserved in place for manual resolution."""
    # First: full migration.
    dat_json = _write(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    db.init_and_migrate(db_path, dat_store_json=dat_json)
    db.engine.dispose()
    db.engine = None
    db.SessionLocal = None

    # Now re-create dat_store.json (different data!) and re-run. DB
    # already has rows → migration should skip and leave JSON intact.
    second_payload = {
        "dats": {"ffffffff": {"id": "ffffffff", "name": "Other", "file_count": 0}},
        "hashes": {"sha1": {}, "md5": {}},
        "matches": {},
    }
    dat_json2 = _write(tmp_path / "dat_store.json", second_payload)
    db.init_and_migrate(db_path, dat_store_json=dat_json2)

    with db.get_session() as s:
        # Still original DATs, not the "Other" one.
        ids = {d.id for d in s.query(db.DAT).all()}
        assert ids == {"abc12345", "def67890"}

    # I2: JSON must not be touched.
    assert dat_json2.exists()
    assert not (tmp_path / "dat_store.json.migrated.bak2").exists()


# ---------------------------------------------------------------------------
# I2 + I5: corrupt / failed inputs
# ---------------------------------------------------------------------------


def test_corrupt_json_is_renamed_to_corrupt_and_does_not_block_others(
    tmp_path: Path, db_path: str,
):
    # dat_store.json is corrupt.
    bad = tmp_path / "dat_store.json"
    bad.write_text("{ not valid json", encoding="utf-8")

    # verified_chds.json is fine.
    good = _write(tmp_path / "verified_chds.json", SAMPLE_VERIFICATION)

    db.init_and_migrate(
        db_path,
        dat_store_json=bad,
        verification_json=good,
    )

    # I5: corrupt file renamed, never imported.
    assert not bad.exists()
    assert (tmp_path / "dat_store.json.corrupt").exists()

    # Good store still migrated successfully.
    assert not good.exists()
    assert (tmp_path / "verified_chds.json.migrated.bak").exists()
    with db.get_session() as s:
        assert s.query(db.DAT).count() == 0
        assert s.query(db.Verification).count() == 2


def test_wrong_toplevel_type_is_skipped_without_side_effects(
    tmp_path: Path, db_path: str,
):
    # Top-level is a list, not a dict.  Should be skipped cleanly.
    weird = tmp_path / "dat_store.json"
    weird.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    db.init_and_migrate(db_path, dat_store_json=weird)

    # I2: source JSON preserved (not corrupt, just wrong shape).
    assert weird.exists()
    assert not (tmp_path / "dat_store.json.migrated.bak").exists()

    with db.get_session() as s:
        assert s.query(db.DAT).count() == 0


def test_missing_json_is_a_clean_noop(tmp_path: Path, db_path: str):
    missing = tmp_path / "dat_store.json"  # never created
    db.init_and_migrate(db_path, dat_store_json=missing)
    # DB exists and is empty.
    with db.get_session() as s:
        assert s.query(db.DAT).count() == 0
    assert not missing.exists()
    assert not (tmp_path / "dat_store.json.migrated.bak").exists()


def test_orphan_hash_entries_are_dropped_with_warning(tmp_path: Path, db_path: str, caplog):
    """A hash row that references a dat_id not in `dats` must not crash
    the migration — it's dropped and a warning is logged."""
    payload = {
        "dats": {
            "keep0001": {"id": "keep0001", "name": "Keep", "file_count": 1},
        },
        "hashes": {
            "sha1": {
                "1" * 40: {
                    "dat_id": "keep0001",
                    "game_name": "Kept", "rom_name": "kept.iso", "size": 1,
                },
                "2" * 40: {
                    "dat_id": "orphaned",  # not in dats!
                    "game_name": "Dropped", "rom_name": "dropped.iso", "size": 1,
                },
            },
            "md5": {},
        },
        "matches": {},
    }
    p = _write(tmp_path / "dat_store.json", payload)
    with caplog.at_level("WARNING", logger="compressatorium.db"):
        db.init_and_migrate(db_path, dat_store_json=p)

    with db.get_session() as s:
        assert s.query(db.DATHash).count() == 1  # orphan dropped
    assert any("hashes migrated" in rec.message for rec in caplog.records)
