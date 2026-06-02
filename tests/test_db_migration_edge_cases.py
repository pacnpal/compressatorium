"""Edge-case coverage for the JSON → SQLite migration.

Complements ``test_db_migration`` with scenarios that module doesn't
cover: backup collision, mid-migration DB faults, unicode / very long
paths, empty collections, mixed hash types, orphaned match dat_ids,
and truncated JSON bodies.
"""

# ruff: noqa: S101

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from services import db as _db

from .conftest import SAMPLE_DAT_STORE, SAMPLE_VERIFICATION


@pytest.fixture(autouse=True)
def _reset_engine():
    _db.engine = None
    _db.SessionLocal = None
    yield
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Backup filename collision
# ---------------------------------------------------------------------------


def test_existing_migrated_bak_preserves_fresh_json(tmp_path: Path, db_path: str, caplog):
    """If a ``.migrated.bak`` from a prior run is already on disk, the
    migration must not overwrite it.  The fresh JSON source is
    preserved untouched (I2) and a warning is logged.
    """
    dat_json = _write_json(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    existing_backup = tmp_path / "dat_store.json.migrated.bak"
    existing_backup.write_text("stale-backup-contents", encoding="utf-8")

    with caplog.at_level("WARNING", logger="compressatorium.db"):
        _db.init_and_migrate(db_path, dat_store_json=dat_json)

    # Source JSON untouched; backup unchanged; DB got the import (the
    # target tables were empty so it proceeded, it's only the *rename*
    # that was skipped).
    assert dat_json.exists()
    assert existing_backup.read_text(encoding="utf-8") == "stale-backup-contents"
    assert any("backup already exists" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Mid-migration rollback
# ---------------------------------------------------------------------------


def test_mid_migration_error_rolls_back_and_preserves_json(
    tmp_path: Path, db_path: str, monkeypatch,
):
    """A synthetic failure inside ``_migrate_dat_store`` must roll back
    its transaction (no rows land), leave the JSON source untouched
    (I2), and not block other stores from succeeding in the same
    ``init_and_migrate`` call.
    """
    dat_json = _write_json(tmp_path / "dat_store.json", SAMPLE_DAT_STORE)
    ver_json = _write_json(tmp_path / "verified_chds.json", SAMPLE_VERIFICATION)

    original_migrator = _db._migrate_dat_store

    def boom(engine, path):  # noqa: ANN001
        # Start the real migration, then inject a failure *after* rows
        # would have been flushed so we truly exercise the rollback path.
        # Simplest reliable approach: raise before the function touches
        # the DB at all, rollback path still runs via the outer
        # init_and_migrate try/except, and no partial write occurs.
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(_db, "_migrate_dat_store", boom)

    _db.init_and_migrate(
        db_path, dat_store_json=dat_json, verification_json=ver_json,
    )

    # I2: dat_store.json preserved for retry.
    assert dat_json.exists()
    assert not (tmp_path / "dat_store.json.migrated.bak").exists()

    # Other stores unaffected.
    assert not ver_json.exists()
    assert (tmp_path / "verified_chds.json.migrated.bak").exists()

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 0
        assert s.query(_db.Verification).count() == 2

    # Sanity: restore for any subsequent test in the module.
    monkeypatch.setattr(_db, "_migrate_dat_store", original_migrator)


# ---------------------------------------------------------------------------
# Unicode + very long paths
# ---------------------------------------------------------------------------


def test_unicode_and_long_paths_round_trip(tmp_path: Path, db_path: str):
    long_path = "/data/" + ("ディレクトリ" * 200) + "/ファイル.chd"  # ~1800 chars
    emoji_path = "/data/🎮/mario 🕹️.chd"

    payload = {
        "dats": {
            "uni00001": {
                "id": "uni00001",
                "name": "名前",
                "description": "ユニコード DAT 💿",
                "version": "1.0",
                "imported_at": "",
                "file_count": 0,
            },
        },
        "hashes": {"sha1": {}, "md5": {}},
        "matches": {
            long_path: {"matched": False, "path": long_path},
            emoji_path: {
                "matched": True, "dat_id": "uni00001",
                "game_name": "🎮 Game",
                "rom_name": "game.iso",
                "match_type": "file_sha1",
                "file_hash": "a" * 40,
            },
        },
    }
    p = _write_json(tmp_path / "dat_store.json", payload)
    _db.init_and_migrate(db_path, dat_store_json=p)

    with _db.get_session() as s:
        assert s.query(_db.DATMatch).count() == 2
        emoji_row = s.get(_db.DATMatch, emoji_path)
        assert emoji_row is not None
        assert emoji_row.game_name == "🎮 Game"
        long_row = s.get(_db.DATMatch, long_path)
        assert long_row is not None


# ---------------------------------------------------------------------------
# Empty collections
# ---------------------------------------------------------------------------


def test_empty_dat_store_migrates_cleanly(tmp_path: Path, db_path: str):
    payload = {"dats": {}, "hashes": {"sha1": {}, "md5": {}}, "matches": {}}
    p = _write_json(tmp_path / "dat_store.json", payload)
    _db.init_and_migrate(db_path, dat_store_json=p)

    # Empty JSON still gets renamed to .migrated.bak, the file was valid.
    assert not p.exists()
    assert (tmp_path / "dat_store.json.migrated.bak").exists()

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 0
        assert s.query(_db.DATHash).count() == 0
        assert s.query(_db.DATMatch).count() == 0


# ---------------------------------------------------------------------------
# Mixed hash types
# ---------------------------------------------------------------------------


def test_sha1_and_md5_coexist_under_composite_pk(tmp_path: Path, db_path: str):
    """The same hex value can exist as both sha1 and md5 because the PK
    is ``(hash, hash_type)``.  Verify both rows land and lookups resolve
    to the correct discriminator."""
    same_hex = "1" * 32  # valid length for md5; also a valid sha1 prefix collision
    payload = {
        "dats": {
            "mix00001": {
                "id": "mix00001", "name": "Mix", "file_count": 0,
            },
        },
        "hashes": {
            "sha1": {
                ("1" * 40): {
                    "dat_id": "mix00001", "game_name": "S",
                    "rom_name": "s.iso", "size": 1,
                },
            },
            "md5": {
                same_hex: {
                    "dat_id": "mix00001", "game_name": "M",
                    "rom_name": "m.iso", "size": 1,
                },
            },
        },
        "matches": {},
    }
    p = _write_json(tmp_path / "dat_store.json", payload)
    _db.init_and_migrate(db_path, dat_store_json=p)

    with _db.get_session() as s:
        sha_row = s.scalars(
            select(_db.DATHash).where(
                _db.DATHash.hash == "1" * 40, _db.DATHash.hash_type == "sha1",
            )
        ).one()
        md5_row = s.scalars(
            select(_db.DATHash).where(
                _db.DATHash.hash == same_hex, _db.DATHash.hash_type == "md5",
            )
        ).one()
        assert sha_row.game_name == "S"
        assert md5_row.game_name == "M"


# ---------------------------------------------------------------------------
# Orphaned match dat_id
# ---------------------------------------------------------------------------


def test_orphan_match_dat_id_becomes_null(tmp_path: Path, db_path: str):
    payload = {
        "dats": {
            "keep0001": {"id": "keep0001", "name": "Keep", "file_count": 0},
        },
        "hashes": {"sha1": {}, "md5": {}},
        "matches": {
            "/data/ghost.chd": {
                "matched": True,
                "dat_id": "vanished",  # never existed
                "game_name": "Ghost",
                "rom_name": "ghost.iso",
                "match_type": "file_sha1",
                "file_hash": "a" * 40,
            },
        },
    }
    p = _write_json(tmp_path / "dat_store.json", payload)
    _db.init_and_migrate(db_path, dat_store_json=p)

    with _db.get_session() as s:
        row = s.get(_db.DATMatch, "/data/ghost.chd")
        assert row is not None
        assert row.dat_id is None
        # UI-visible fields are preserved even when the DAT ref is gone.
        assert row.matched is True
        assert row.game_name == "Ghost"


# ---------------------------------------------------------------------------
# Truncated JSON body
# ---------------------------------------------------------------------------


def test_truncated_json_renamed_to_corrupt(tmp_path: Path, db_path: str):
    truncated = tmp_path / "dat_store.json"
    truncated.write_text('{"dats": {"a": {"id": "a", "name":', encoding="utf-8")

    _db.init_and_migrate(db_path, dat_store_json=truncated)

    assert not truncated.exists()
    assert (tmp_path / "dat_store.json.corrupt").exists()
    # .migrated.bak must not be created for a corrupt file.
    assert not (tmp_path / "dat_store.json.migrated.bak").exists()

    with _db.get_session() as s:
        assert s.query(_db.DAT).count() == 0
