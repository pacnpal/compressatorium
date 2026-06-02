import asyncio
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
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "is_disc_id_checked", fake_is_disc_id_checked
    )
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "mark_disc_id_checked", fake_mark_disc_id_checked
    )
    monkeypatch.setattr(info_routes, "disc_id_ensure_embedded", fake_ensure_embedded)

    return {
        # Discovery realpath-normalizes paths, so the values the scan passes
        # downstream are the resolved form; assert against that.
        "chd_path": os.path.realpath(str(chd_path)),
        "calls": calls,
        "ensure_calls": ensure_calls,
        "disc_id_checked_paths": disc_id_checked_paths,
        "marked_paths": marked_paths,
    }


@pytest.mark.asyncio
async def test_scan_metadata_force_ignores_cache(scan_env, monkeypatch):
    async def fake_false(_):
        return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=True)

    assert set(scan_env["calls"]) == {scan_env["chd_path"]}


@pytest.mark.asyncio
async def test_scan_metadata_respects_cache(scan_env, monkeypatch):
    async def fake_false(_):
        return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    await info_routes.scan_metadata_task(force=False)

    assert scan_env["calls"] == []


@pytest.mark.asyncio
async def test_scan_metadata_retroactive_tagging_runs_for_all(scan_env, monkeypatch):
    """Phase 2 runs for CHDs not yet marked as disc-id-checked."""
    async def fake_false(_):
        return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    # is_disc_id_checked returns False (not yet checked) → Phase 2 runs
    await info_routes.scan_metadata_task(force=False)

    assert scan_env["calls"] == []  # phase 1: no info refresh (cache fresh)
    assert scan_env["ensure_calls"] == [scan_env["chd_path"]]  # phase 2: ran
    assert scan_env["marked_paths"] == [scan_env["chd_path"]]  # marked after run


@pytest.mark.asyncio
async def test_scan_metadata_skips_disc_id_already_checked(scan_env, monkeypatch):
    """Phase 2 skips CHDs that are already marked as disc-id-checked (mtime unchanged)."""
    async def fake_false(_):
        return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", fake_false)

    # Pre-mark the CHD as already checked
    scan_env["disc_id_checked_paths"].add(scan_env["chd_path"])

    await info_routes.scan_metadata_task(force=False)

    assert scan_env["ensure_calls"] == []  # phase 2: skipped
    assert scan_env["marked_paths"] == []  # not re-marked


@pytest.mark.asyncio
async def test_scan_metadata_cancellation_flips_to_cancelled(tmp_path, monkeypatch):
    """Mid-loop cancel during Phase 1 finishes the scan with CANCELLED status
    and preserves any metadata already extracted before cancel fired."""
    from services.job_manager import job_manager

    # Three CHD files so we have room to cancel mid-loop.
    chd_paths = []
    for name in ("a.chd", "b.chd", "c.chd"):
        p = tmp_path / name
        p.write_text("x")
        chd_paths.append(str(p))

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    # Force Phase 1 to run for every path.
    async def always_stale(_):
        return True
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", always_stale)

    processed_paths: list[str] = []
    scan_job_ids: list[str] = []

    # Capture the scan_job_id as soon as create_external_job is called so we
    # can cancel it from inside fake_info.
    original_create = info_routes.job_manager.create_external_job

    def capture_create(*args, **kwargs):
        job = original_create(*args, **kwargs)
        scan_job_ids.append(job.id)
        return job
    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture_create)

    async def fake_info(path):
        processed_paths.append(path)
        # After the first file's metadata is extracted, request cancel.
        # The next iteration's top-of-loop check trips ExternalJobCancelled.
        if len(processed_paths) == 1 and scan_job_ids:
            await job_manager.cancel_job(scan_job_ids[0])
        return {"raw_data": "Tag: CD-ROM"}

    async def fake_set_metadata(path, info, persist=False):
        return {"media_type": "cd"}

    async def fake_flush_async():
        return None

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "set_metadata", fake_set_metadata)
    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)

    await info_routes.scan_metadata_task(force=True)

    assert scan_job_ids, "scan_metadata_task should have created an external job"
    scan_job_id = scan_job_ids[0]
    final = job_manager.jobs[scan_job_id]
    assert final.status.value == "cancelled"
    assert "Cancelled" in final.message
    # Exactly one file finished before cancel; the next two never ran.
    assert len(processed_paths) == 1


@pytest.mark.asyncio
async def test_scan_metadata_cancellation_during_phase2(tmp_path, monkeypatch):
    """Phase 2 (disc-id loop) has its own cancel check, verify it works
    when Phase 1 was a no-op (everything cache-fresh)."""
    from services.job_manager import job_manager

    for name in ("a.chd", "b.chd", "c.chd"):
        (tmp_path / name).write_text("x")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    # Phase 1 is skipped entirely, nothing is stale.
    async def never_stale(_):
        return False
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", never_stale)

    # Phase 2 runs for all paths (none are marked disc-id-checked).
    async def not_checked(_):
        return False
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "is_disc_id_checked", not_checked,
    )

    async def mark_checked(_):
        return None
    monkeypatch.setattr(
        info_routes.chd_metadata_store, "mark_disc_id_checked", mark_checked,
    )

    async def fake_flush_async():
        return None
    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)

    ensure_calls: list[str] = []
    scan_job_ids: list[str] = []
    original_create = info_routes.job_manager.create_external_job

    def capture_create(*args, **kwargs):
        job = original_create(*args, **kwargs)
        scan_job_ids.append(job.id)
        return job
    monkeypatch.setattr(info_routes.job_manager, "create_external_job", capture_create)

    async def fake_ensure_embedded(path, chdman_path):
        ensure_calls.append(path)
        # Cancel after the first disc-id scan; next iteration's top-of-loop
        # check raises ExternalJobCancelled.
        if len(ensure_calls) == 1 and scan_job_ids:
            await job_manager.cancel_job(scan_job_ids[0])
        return None
    monkeypatch.setattr(info_routes, "disc_id_ensure_embedded", fake_ensure_embedded)

    await info_routes.scan_metadata_task(force=False)

    scan_job_id = scan_job_ids[0]
    final = job_manager.jobs[scan_job_id]
    assert final.status.value == "cancelled"
    assert "Cancelled" in final.message
    assert len(ensure_calls) == 1


@pytest.mark.asyncio
async def test_scan_discovers_non_chd_and_keeps_phases_chd_only(tmp_path, monkeypatch):
    """Discovery is registry-driven: the scan walks non-CHD outputs too, but
    Phase 1/2 (chdman info + disc-id) only ever touch the .chd files."""
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    (tmp_path / "game.chd").write_text("x")
    (tmp_path / "disc.rvz").write_text("y")
    (tmp_path / "rom.nsz").write_text("z")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    info_calls: list[str] = []

    async def fake_info(path):
        info_calls.append(path)
        return {"raw_data": "Tag: CD-ROM"}

    async def fake_set_metadata(path, info, persist=False):
        return {"media_type": "cd"}

    async def fake_flush_async():
        return None

    async def always_stale(_):
        return True

    async def already_checked(_):
        return True  # skip Phase 2 work

    monkeypatch.setattr(info_routes.chdman_service, "info", fake_info)
    monkeypatch.setattr(info_routes.chd_metadata_store, "set_metadata", fake_set_metadata)
    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", always_stale)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_disc_id_checked", already_checked)

    # Phase 3: DATs present; capture which paths get matched + cached.
    monkeypatch.setattr(global_dat_store, "has_dats", lambda: True)
    monkeypatch.setattr(global_dat_store, "get_matches_batch", lambda paths: {})

    set_match_paths: list[str] = []

    async def fake_set_match(path, match):
        set_match_paths.append(path)

    monkeypatch.setattr(global_dat_store, "set_match", fake_set_match)

    matched_paths: list[str] = []

    async def fake_match_single(path):
        matched_paths.append(path)
        return {"path": path, "matched": True, "match_type": "file_sha1"}

    monkeypatch.setattr(dat_internal, "_match_single_file", fake_match_single)

    await info_routes.scan_metadata_task(force=True)

    # Phase 1 (chdman info) only ran for the CHD, never the .rvz/.nsz.
    # Discovery realpath-normalizes, so compare against the resolved forms.
    assert info_calls == [os.path.realpath(str(tmp_path / "game.chd"))]
    # Phase 3 visited every discovered output regardless of format.
    expected = {
        os.path.realpath(str(tmp_path / "game.chd")),
        os.path.realpath(str(tmp_path / "disc.rvz")),
        os.path.realpath(str(tmp_path / "rom.nsz")),
    }
    assert set(matched_paths) == expected
    # Cacheable results were persisted for all of them.
    assert set(set_match_paths) == expected


@pytest.mark.asyncio
async def test_scan_phase3_skips_when_no_dats(tmp_path, monkeypatch):
    """Phase 3 is a no-op (no hashing) when no DATs are imported."""
    import routes.dat as dat_internal
    from services.dat_store import dat_store as global_dat_store

    (tmp_path / "disc.rvz").write_text("y")

    monkeypatch.setattr(info_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(info_routes.settings, "data_mount_root", str(tmp_path))

    async def fake_flush_async():
        return None

    async def never_stale(_):
        return False

    monkeypatch.setattr(info_routes.chd_metadata_store, "flush_async", fake_flush_async)
    monkeypatch.setattr(info_routes.chd_metadata_store, "is_stale", never_stale)
    monkeypatch.setattr(global_dat_store, "has_dats", lambda: False)

    called = []

    async def fake_match_single(path):
        called.append(path)
        return {"path": path, "matched": False}

    monkeypatch.setattr(dat_internal, "_match_single_file", fake_match_single)

    await info_routes.scan_metadata_task(force=True)

    assert called == []


@pytest.fixture
def metadata_store_path(tmp_path):
    # SQLite file per test. Name kept as "chd_metadata.db" for clarity.
    return tmp_path / "chd_metadata.db"


@pytest.fixture
def metadata_store(metadata_store_path):
    return CHDMetadataStore(str(metadata_store_path))


def test_db_path_resolution_falls_back_when_default_config_unwritable(monkeypatch, tmp_path):
    """Data-dir fallback semantics (moved out of the old JSON store)."""
    from services.db import resolve_db_path

    original_mkdir = Path.mkdir

    def guarded_mkdir(self, *args, **kwargs):
        if os.fspath(self) == "/config":
            raise OSError(30, "Read-only file system")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    # No explicit path, default data_dir=/config, should fall back to TMPDIR.
    resolved = resolve_db_path(None, data_dir="/config")
    assert Path(resolved).parent == tmp_path / "compressatorium"
    assert Path(resolved).name == "compressatorium.db"


@pytest.mark.asyncio
async def test_concurrent_metadata_writes(metadata_store, tmp_path):
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

    # Verify all records are present in the DB.
    records = metadata_store.all_records()
    assert len(records) == count
    # Spot check: each normalized path is present exactly once.
    real_paths = {os.path.realpath(p) for p in paths}
    assert {r["chd_path"] for r in records} == real_paths


@pytest.mark.asyncio
async def test_metadata_persist_version_gate(metadata_store, tmp_path):
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

    # Both should be present in the DB.
    real_a = os.path.realpath(path_a)
    real_b = os.path.realpath(path_b)

    paths_on_disk = {r["chd_path"] for r in metadata_store.all_records()}
    assert real_a in paths_on_disk
    assert real_b in paths_on_disk


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

    # Should be False, file changed since last check
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

    # No set_metadata call, no existing record
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

    # Phase 1 refreshes CHD metadata, must not erase the disc-id-checked flag
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

    # No set_metadata call prior, record doesn't exist
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

    # Phase 1 metadata refresh, must not erase the cached disc-ID fields
    await metadata_store.set_metadata(path, {"raw_data": "Tag: DVD-VIDEO"}, persist=False)

    game_id, title = await metadata_store.get_disc_id_info(path)
    assert game_id == "SLUS-20312"
    assert title == "God of War"


@pytest.mark.asyncio
async def test_update_disc_id_info_persist_false_does_not_flush(
    metadata_store, tmp_path, monkeypatch,
):
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
    """Phase 2 calls update_disc_id_info(persist=False) when ensure_disc_id_embedded returns a
    game_id.

    Regression test: Phase 2 previously embedded the GAME/NAME tags into the CHD file
    but never populated the metadata cache, so subsequent /api/info requests returned
    no game_id even though the tag was physically in the file.
    """
    async def fake_false(_):
        return False
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
