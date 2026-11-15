"""Tests for verification store persistence and concurrency behavior."""

# ruff: noqa: S101

import asyncio
import os
from pathlib import Path

import pytest

from app.services.verification_store import VerificationStore


@pytest.fixture(name="store_path")
def _store_path(tmp_path: Path) -> Path:
    """Per-test SQLite path for the verification store."""
    return tmp_path / "verifications.db"


@pytest.fixture(name="verification_store")
def _verification_store(store_path: Path) -> VerificationStore:
    """Create a verification store bound to a temporary SQLite DB."""
    return VerificationStore(str(store_path))


def test_verification_store_defaults_to_no_session_when_db_uninitialized():
    """A bare VerificationStore() with no args and no db.init_engine()
    must not touch the filesystem at construction time.  Accessing it
    *later* should raise a clear error rather than corrupt state."""
    from services import db as _db

    original = _db.SessionLocal
    _db.SessionLocal = None
    try:
        store = VerificationStore()  # no store_path, no session_factory
        with pytest.raises(RuntimeError, match="SessionLocal not initialized"):
            store._all_records_sync()
    finally:
        _db.SessionLocal = original


@pytest.mark.asyncio
async def test_async_mark_verified(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    """Marking a CHD persists the record and resolves the real path."""
    path = str(tmp_path / "test.chd")
    real_path = os.path.realpath(path)
    await verification_store.mark_verified(path)

    assert await verification_store.is_verified(path)
    assert await verification_store.is_verified(real_path)

    record = await verification_store.get_record(path)
    assert record is not None
    assert record["chd_path"] == real_path


@pytest.mark.asyncio
async def test_concurrent_writes(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    """Simultaneous mark_verified calls all persist without data loss."""
    count = 50
    paths = [str(tmp_path / f"file_{i}.chd") for i in range(count)]
    real_paths = {os.path.realpath(p) for p in paths}

    await asyncio.gather(*[verification_store.mark_verified(p) for p in paths])

    records = await verification_store.all_records()
    assert len(records) == count
    assert {r["chd_path"] for r in records} == real_paths


@pytest.mark.asyncio
async def test_race_condition_persistence(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    """Interleaved writes to two different paths both durably persist."""
    path_a = str(tmp_path / "a.chd")
    path_b = str(tmp_path / "b.chd")
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)

    async def spam_b() -> None:
        for _ in range(20):
            await verification_store.mark_verified(path_b)
            await asyncio.sleep(0.001)

    await asyncio.gather(
        verification_store.mark_verified(path_a),
        spam_b(),
    )

    assert await verification_store.is_verified(path_a)
    assert await verification_store.is_verified(path_b)

    paths_on_disk = {r["chd_path"] for r in await verification_store.all_records()}
    assert real_a in paths_on_disk
    assert real_b in paths_on_disk


@pytest.mark.asyncio
async def test_clear_removes_record(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "file.chd")
    await verification_store.mark_verified(path)
    assert await verification_store.is_verified(path)
    await verification_store.clear(path)
    assert not await verification_store.is_verified(path)


@pytest.mark.asyncio
async def test_move_preserves_metadata(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    old = str(tmp_path / "old.chd")
    new = str(tmp_path / "new.chd")
    await verification_store.mark_verified(old, source_path=str(tmp_path / "src.iso"))
    await verification_store.move(old, new)

    assert not await verification_store.is_verified(old)
    assert await verification_store.is_verified(new)
    record = await verification_store.get_record(new)
    assert record is not None
    assert record["source_path"] == os.path.realpath(str(tmp_path / "src.iso"))


@pytest.mark.asyncio
async def test_concurrent_writers_under_wal(
    verification_store: VerificationStore,
    tmp_path: Path,
) -> None:
    """Two tasks hammering distinct paths must both land without a
    ``database is locked`` error.  WAL + busy_timeout=30s is the whole
    point of the engine's PRAGMA hook, this test proves they take
    effect end-to-end through ``mark_verified``."""
    n = 200

    async def writer(prefix: str) -> None:
        for i in range(n):
            await verification_store.mark_verified(
                str(tmp_path / f"{prefix}_{i:04d}.chd")
            )

    await asyncio.gather(writer("a"), writer("b"))

    # Every write was distinct → exactly 2*n rows.
    all_records = await verification_store.all_records()
    assert len(all_records) == 2 * n



@pytest.mark.asyncio
async def test_refreshing_a_check_keeps_its_provenance(db_path, reset_db_engine):
    """codex 4576728584, review r8 C1: a plain re-check (CLI verify, the web's
    Verify) never erases the mode that made the file or its Source."""
    from services import db as _db
    from services.verification_store import verification_store

    _db.init_engine(db_path)
    try:
        await verification_store.mark_verified(
            "/data/ps3/Game.iso", source_path="/data/ps3/GAME",
            produced_meta={"mode": "folder_to_iso"}, check="round_tripped",
        )
        await verification_store.refresh_check("/data/ps3/Game.iso")
        record = await verification_store.get_record("/data/ps3/Game.iso")
        assert record["produced_meta"] == {"mode": "folder_to_iso"}
        assert record["source_path"] == os.path.realpath("/data/ps3/GAME")
        assert record["check_passed"] == "intact"
        # A file checked for the first time gets a plain record.
        await verification_store.refresh_check("/data/gc/New.rvz")
        assert (await verification_store.get_record("/data/gc/New.rvz"))["produced_meta"] is None
    finally:
        reset_db_engine()


@pytest.mark.asyncio
async def test_a_new_conversion_replaces_stale_provenance(db_path, reset_db_engine):
    """review r9 M2: an output deleted outside the app and rebuilt at the same
    path by another mode must not inherit the old mode, even when the new
    conversion records none."""
    from services import db as _db
    from services.verification_store import verification_store

    _db.init_engine(db_path)
    try:
        await verification_store.mark_verified(
            "/data/gc/Game.iso", source_path="/data/ps3/GAME",
            produced_meta={"mode": "folder_to_iso"},
        )
        await verification_store.mark_verified("/data/gc/Game.iso")
        record = await verification_store.get_record("/data/gc/Game.iso")
        assert record["produced_meta"] is None
        assert record["source_path"] is None
        await verification_store.mark_verified(
            "/data/gc/Game.iso", produced_meta={"mode": "dolphin_iso"},
        )
        record = await verification_store.get_record("/data/gc/Game.iso")
        assert record["produced_meta"] == {"mode": "dolphin_iso"}
    finally:
        reset_db_engine()


@pytest.mark.asyncio
async def test_all_provenance_includes_failed_rows(db_path, reset_db_engine):
    """all_provenance lists every row, failed ones too; all_records doesn't."""
    from services import db as _db
    from services.verification_store import verification_store

    _db.init_engine(db_path)
    try:
        await verification_store.mark_verified(
            "/data/ps3/Game.iso", produced_meta={"mode": "folder_to_iso"},
        )
        await verification_store.mark_failed("/data/ps3/Game.iso")
        provenance = await verification_store.all_provenance()
        assert [(r["chd_path"], r["produced_meta"]) for r in provenance] == [
            (os.path.realpath("/data/ps3/Game.iso"), {"mode": "folder_to_iso"}),
        ]
        assert await verification_store.all_records() == []
    finally:
        reset_db_engine()
