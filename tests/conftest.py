"""Shared fixtures and sample payloads for the DB / migration test suite.

Centralised here so the four DB-focused test modules
(``test_db_migration``, ``test_db_startup_integration``,
``test_db_engine_pragmas``, ``test_db_store_operations``,
``test_db_migration_edge_cases``) don't drift apart or duplicate
boilerplate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services import db as _db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh SQLite file per test (on-disk, not :memory:, because migration
    tests assert persistence across engine disposals)."""
    return str(tmp_path / "compressatorium.db")


@pytest.fixture
def reset_db_engine():
    """Explicit reset helper for tests that re-init the engine mid-test.

    The autouse ``_reset_engine`` fixture in individual test modules
    handles the before/after-test cleanup.  This fixture returns a
    zero-arg callable for tests that need to simulate a process
    restart: ``reset()`` disposes the current engine and clears the
    module-level ``SessionLocal`` so the next ``init_engine`` starts
    fresh.
    """
    def _reset() -> None:
        if _db.engine is not None:
            _db.engine.dispose()
        _db.engine = None
        _db.SessionLocal = None

    return _reset


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
