import asyncio
import json
import os
from pathlib import Path

import pytest

from app.routes import info as info_routes
from app.services.chd_metadata_store import CHDMetadataStore


@pytest.fixture
def media_type_cases():
    return [
        ({"raw_data": "Metadata: CHCD, Tag: CD-ROM"}, "cd"),
        ({"raw_data": "Tag: DVD-VIDEO"}, "dvd"),
        ({"raw_data": "Tag: GD-ROM"}, "cd"),
        ({"metadata_lines": ["CHCD"]}, "cd"),
        ({"metadata_lines": ["CHT2"]}, "cd"),  # CHT2 is a CD identifier
        ({"raw_data": "Metadata: CHTR"}, "cd"),  # CHTR prefix
        ({"raw_data": "Tag: DVD-ROM"}, "dvd"),
        # Real-world chdman output format with quoted tag
        ({"raw_data": "Metadata:     Tag='CHT2'  Index=0  Length=93 bytes"}, "cd"),
        ({"raw_data": "Tag='CHCD'"}, "cd"),
    ]


def test_extract_media_type_cases(media_type_cases):
    for info, expected in media_type_cases:
        assert CHDMetadataStore.extract_media_type(info) == expected


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    chd_path = tmp_path / "game.chd"
    chd_path.write_text("test")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    calls = []
    ensure_calls = []
    disc_id_checked_paths: set[str] = set()
    marked_paths: list[str] = []

    async def fake_info(path):
        calls.append(path)
        return {"raw_data": "Tag: CD-ROM"}

    async def fake_set_metadata(path, info, persist=False):
        return {"media_type": "cd"}

    async def fake_flush_async():
        return None

    async def fake_ensure_embedded(path, chdman_path):
        ensure_calls.append(path)
        return None

    async def fake_is_disc_id_checked(path):
        return path in disc_id_checked_paths

    async def fake_mark_disc_id_checked(path):
        marked_paths.append(path)

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "set_metadata", fake_set_metadata)
    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_disc_id_checked", fake_is_disc_id_checked)
    monkeypatch.setattr(info_routes.chd_metadata_store, "mark_disc_id_checked", fake_mark_disc_id_checked)
    monkeypatch.setattr(info_routes, "disc_id_ensure_embedded", fake_ensure_embedded)

    return {
        "chd_path": str(chd_path),
        "calls": calls,
        "ensure_calls": ensure_calls,
        "disc_id_checked_paths": disc_id_checked_paths,
        "marked_paths": marked_paths,
    }


@pytest.mark.asyncio
async def test_scan_metadata_force_ignores_cache(scan_env, monkeypatch):
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=True)

    assert set(scan_env["calls"]) == {scan_env["chd_path"]}


@pytest.mark.asyncio
async def test_scan_metadata_respects_cache(scan_env, monkeypatch):
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=False)

    assert scan_env["calls"] == []


@pytest.mark.asyncio
async def test_scan_metadata_retroactive_tagging_runs_for_all(scan_env, monkeypatch):
    """Phase 2 runs for CHDs not yet marked as disc-id-checked."""
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    # is_disc_id_checked returns False (not yet checked) → Phase 2 runs
    await info_routes.scan_metadata_task(force=False)

    assert scan_env["calls"] == []  # phase 1: no info refresh (cache fresh)
    assert scan_env["ensure_calls"] == [scan_env["chd_path"]]  # phase 2: ran
    assert scan_env["marked_paths"] == [scan_env["chd_path"]]  # marked after run


@pytest.mark.asyncio
async def test_scan_metadata_skips_disc_id_already_checked(scan_env, monkeypatch):
    """Phase 2 skips CHDs that are already marked as disc-id-checked (mtime unchanged)."""
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    # Pre-mark the CHD as already checked
    scan_env["disc_id_checked_paths"].add(scan_env["chd_path"])

    await info_routes.scan_metadata_task(force=False)

    assert scan_env["ensure_calls"] == []  # phase 2: skipped
    assert scan_env["marked_paths"] == []  # not re-marked


@pytest.fixture
def metadata_store_path(tmp_path):
    return tmp_path / "chd_metadata.json"


@pytest.fixture
def metadata_store(metadata_store_path):
    return CHDMetadataStore(str(metadata_store_path))


def test_metadata_store_falls_back_when_default_config_unwritable(monkeypatch, tmp_path):
    monkeypatch.delenv("CHD_METADATA_STORE", raising=False)
    monkeypatch.delenv("CHD_DATA_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    original_mkdir = Path.mkdir

    def guarded_mkdir(self, *args, **kwargs):
        if os.fspath(self) == "/config":
            raise OSError(30, "Read-only file system")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    store = CHDMetadataStore()

    assert store._store_path.parent == tmp_path / "compressatorium"
    assert store._store_path.name == "chd_metadata.json"


@pytest.mark.asyncio
async def test_concurrent_metadata_writes(metadata_store, metadata_store_path, tmp_path):
    """Test that concurrent writes don't lose data (last-write-wins).
    Simulates the race condition where multiple set_metadata calls
    happen concurrently.
    """
    count = 50
    paths = [str(tmp_path / f"game_{i}.chd") for i in range(count)]
    # Create fake files so realpath works
    for p in paths:
        open(p, "w").close()

    async def set_one(p):
        await metadata_store.set_metadata(p, {"raw_data": f"Tag: CD-ROM for {p}"}, persist=True)

    await asyncio.gather(*[set_one(p) for p in paths])

    # Verify all records are in memory
    records = metadata_store.all_records()
    assert len(records) == count

    # Verify disk state is consistent
    with open(metadata_store_path) as f:
        data = json.load(f)
        assert len(data) == count


@pytest.mark.asyncio
async def test_metadata_persist_version_gate(metadata_store, metadata_store_path, tmp_path):
    """Test that version-gated replace prevents stale overwrites.
    """
    path_a = str(tmp_path / "a.chd")
    path_b = str(tmp_path / "b.chd")
    open(path_a, "w").close()
    open(path_b, "w").close()

    # Set A
    await metadata_store.set_metadata(path_a, {"raw_data": "A"}, persist=True)

    # Spam B while A might still be writing
    async def spam_b():
        for _ in range(20):
            await metadata_store.set_metadata(path_b, {"raw_data": "B"}, persist=True)
            await asyncio.sleep(0.001)

    await asyncio.gather(
        metadata_store.set_metadata(path_a, {"raw_data": "A2"}, persist=True),
        spam_b(),
    )

    # Both should be present
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)

    with open(metadata_store_path) as f:
        data = json.load(f)
        assert real_a in data
        assert real_b in data


@pytest.mark.asyncio
async def test_mark_and_check_disc_id(metadata_store, tmp_path):
    """mark_disc_id_checked → is_disc_id_checked returns True for unchanged file."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    assert not await metadata_store.is_disc_id_checked(path)

    await metadata_store.mark_disc_id_checked(path)

    assert await metadata_store.is_disc_id_checked(path)


@pytest.mark.asyncio
async def test_disc_id_checked_invalidated_on_mtime_change(metadata_store, tmp_path):
    """is_disc_id_checked returns False when file mtime changes after marking."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    await metadata_store.mark_disc_id_checked(path)
    assert await metadata_store.is_disc_id_checked(path)

    # Bump the mtime by 2 s via os.utime so the change is visible even on
    # filesystems with 1-second mtime resolution (avoids a flaky sleep).
    stat = chd.stat()
    os.utime(chd, (stat.st_atime, stat.st_mtime + 2))
    chd.write_text("modified")

    # Should be False — file changed since last check
    assert not await metadata_store.is_disc_id_checked(path)


@pytest.mark.asyncio
async def test_disc_id_checked_missing_file(metadata_store, tmp_path):
    """is_disc_id_checked returns False for a file that does not exist."""
    path = str(tmp_path / "nonexistent.chd")
    assert not await metadata_store.is_disc_id_checked(path)


@pytest.mark.asyncio
async def test_mark_disc_id_checked_creates_minimal_record(metadata_store, tmp_path):
    """mark_disc_id_checked creates a record even when no info was cached yet."""
    chd = tmp_path / "fresh.chd"
    chd.write_text("fake")
    path = str(chd)

    # No set_metadata call — no existing record
    await metadata_store.mark_disc_id_checked(path)

    # Record should exist and report as checked
    assert await metadata_store.is_disc_id_checked(path)


@pytest.mark.asyncio
async def test_set_metadata_preserves_disc_id_checked(metadata_store, tmp_path):
    """set_metadata must not erase disc_id_checked fields from an existing record."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    # Phase 2 marks the CHD as disc-id-checked
    await metadata_store.mark_disc_id_checked(path)
    assert await metadata_store.is_disc_id_checked(path)

    # Phase 1 refreshes CHD metadata — must not erase the disc-id-checked flag
    await metadata_store.set_metadata(path, {"raw_data": "Tag: DVD-VIDEO"}, persist=False)

    # Flag must still be set after the metadata refresh
    assert await metadata_store.is_disc_id_checked(path)


@pytest.mark.asyncio
async def test_get_and_update_disc_id_info(metadata_store, tmp_path):
    """update_disc_id_info stores game_id/title; get_disc_id_info retrieves them."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    # Nothing stored yet
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id is None
    assert title is None

    # Store disc-id info
    await metadata_store.update_disc_id_info(path, "SLUS-20312", "God of War")
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SLUS-20312"
    assert title == "God of War"


@pytest.mark.asyncio
async def test_update_disc_id_info_no_title(metadata_store, tmp_path):
    """update_disc_id_info works when title is None."""
    chd = tmp_path / "ps2game.chd"
    chd.write_text("fake")
    path = str(chd)

    await metadata_store.update_disc_id_info(path, "SCES-50330", None)
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SCES-50330"
    assert title is None


@pytest.mark.asyncio
async def test_update_disc_id_info_creates_stub_record(metadata_store, tmp_path):
    """update_disc_id_info creates a minimal record even when no info was cached yet."""
    chd = tmp_path / "fresh.chd"
    chd.write_text("fake")
    path = str(chd)

    # No set_metadata call prior — record doesn't exist
    await metadata_store.update_disc_id_info(path, "ULES-00135", "Patapon")
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "ULES-00135"
    assert title == "Patapon"


@pytest.mark.asyncio
async def test_set_metadata_preserves_game_id_and_title(metadata_store, tmp_path):
    """set_metadata must not erase game_id/title cached by update_disc_id_info."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    # Cache disc-id info (as the /api/info route would after a cache miss)
    await metadata_store.update_disc_id_info(path, "SLUS-20312", "God of War")
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SLUS-20312"
    assert title == "God of War"

    # Phase 1 metadata refresh — must not erase the cached disc-ID fields
    await metadata_store.set_metadata(path, {"raw_data": "Tag: DVD-VIDEO"}, persist=False)

    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SLUS-20312"
    assert title == "God of War"


@pytest.mark.asyncio
async def test_update_disc_id_info_persist_false_does_not_flush(metadata_store, tmp_path, monkeypatch):
    """update_disc_id_info(persist=False) stores in memory without triggering a disk flush."""
    chd = tmp_path / "game.chd"
    chd.write_text("fake")
    path = str(chd)

    persist_calls: list = []

    async def fake_persist_async():
        persist_calls.append(1)

    monkeypatch.setattr(metadata_store, "_persist_async", fake_persist_async)

    await metadata_store.update_disc_id_info(path, "SLUS-20312", "God of War", persist=False)

    # Data is in memory
    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SLUS-20312"
    assert title == "God of War"
    # No flush triggered
    assert persist_calls == []


@pytest.mark.asyncio
async def test_scan_phase2_caches_game_id_when_disc_id_found(scan_env, monkeypatch):
    """Phase 2 calls update_disc_id_info(persist=False) when ensure_disc_id_embedded returns a game_id.

    Regression test: Phase 2 previously embedded the GAME/NAME tags into the CHD file
    but never populated the metadata cache, so subsequent /api/info requests returned
    no game_id even though the tag was physically in the file.
    """
    async def fake_false(_): return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    # Stub ensure_disc_id_embedded to return a found game_id
    async def fake_ensure_with_result(path, chdman_path):
        scan_env["ensure_calls"].append(path)
        return {"game_id": "SLUS-20312", "title": "God of War"}

    monkeypatch.setattr(info_routes, "disc_id_ensure_embedded", fake_ensure_with_result)

    # Track update_disc_id_info calls (game_id, title, persist)
    update_calls: list[tuple] = []

    async def fake_update_disc_id_info(path, game_id, title, persist=True):
        update_calls.append((game_id, title, persist))

    monkeypatch.setattr(
        info_routes.chd_metadata_store, "update_disc_id_info", fake_update_disc_id_info
    )

    await info_routes.scan_metadata_task(force=False)

    # ensure_disc_id_embedded was called
    assert scan_env["ensure_calls"] == [scan_env["chd_path"]]
    # update_disc_id_info was called with the returned game_id/title and persist=False
    assert len(update_calls) == 1
    assert update_calls[0] == ("SLUS-20312", "God of War", False)
    # CHD was marked as checked
    assert scan_env["marked_paths"] == [scan_env["chd_path"]]
