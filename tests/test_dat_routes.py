"""Tests for MAME Redump DAT file management routes."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes import dat as dat_routes

# Minimal valid Logiqx XML DAT for testing
SAMPLE_DAT_XML = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Test Redump DAT</name>
    <description>Test DAT for unit tests</description>
    <version>1.0</version>
  </header>
  <game name="Test Game">
    <rom name="test.iso" size="737280000"
         sha1="aabbccddaabbccddaabbccddaabbccddaabbccdd"
         md5="aabbccddaabbccddaabbccddaabbccdd"/>
  </game>
</datafile>
"""

SECOND_DAT_XML = """\
<?xml version="1.0"?>
<datafile>
  <header>
    <name>Second DAT</name>
    <description>Another DAT</description>
    <version>2.0</version>
  </header>
  <game name="Another Game">
    <rom name="another.iso" size="1024"
         sha1="1122334455667788990011223344556677889900"
         md5="11223344556677889900112233445566"/>
  </game>
</datafile>
"""


def _make_upload_file(content: str, filename: str = "test.dat"):
    """Build a minimal UploadFile-like mock."""
    encoded = content.encode("utf-8")
    chunks = [encoded[i:i + 65536] for i in range(0, len(encoded), 65536)] + [b""]
    mock_file = MagicMock()
    mock_file.filename = filename
    mock_file.read = AsyncMock(side_effect=chunks)
    return mock_file


@pytest.fixture
def isolated_dat_store(tmp_path, monkeypatch):
    """Provide a fresh DATStore backed by a temp file for each test."""
    from services.dat_store import DATStore
    store = DATStore(store_path=str(tmp_path / "dat_store.json"))
    monkeypatch.setattr(dat_routes, "dat_store", store)
    return store


# ---------------------------------------------------------------------------
# /dat/import
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_dat_happy_path(isolated_dat_store):
    """Importing a valid DAT returns summary and populates the store."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    result = await dat_routes.import_dat(file=upload)

    assert result["name"] == "Test Redump DAT"
    assert result["version"] == "1.0"
    assert result["file_count"] == 1
    assert result["hashes_added"] == 2  # 1 SHA1 + 1 MD5

    dats = isolated_dat_store.list_dats()
    assert len(dats) == 1
    assert dats[0]["name"] == "Test Redump DAT"


@pytest.mark.asyncio
async def test_import_dat_wrong_extension():
    """Uploading a file with a disallowed extension is rejected (400)."""
    upload = _make_upload_file(SAMPLE_DAT_XML, filename="test.txt")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.import_dat(file=upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_dat_invalid_xml(isolated_dat_store):
    """Uploading malformed XML is rejected with 400."""
    upload = _make_upload_file("this is not xml", filename="bad.dat")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.import_dat(file=upload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_dat_clears_match_cache(isolated_dat_store):
    """Importing a new DAT must clear the stale _matches cache."""
    # Seed a stale "unmatched" cache entry
    await isolated_dat_store.set_match("/some/file.iso", {"path": "/some/file.iso", "matched": False})
    assert isolated_dat_store.get_match("/some/file.iso") is not None

    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Cache should have been cleared so the stale entry is gone
    assert isolated_dat_store.get_match("/some/file.iso") is None


# ---------------------------------------------------------------------------
# /dat/list  and  /dat/{dat_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_dats_empty(isolated_dat_store):
    """list_dats returns an empty list when no DATs are imported."""
    result = await dat_routes.list_dats()
    assert result == []


@pytest.mark.asyncio
async def test_list_dats_after_import(isolated_dat_store):
    """list_dats returns imported DAT entries."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    result = await dat_routes.list_dats()
    assert len(result) == 1
    assert result[0]["name"] == "Test Redump DAT"


@pytest.mark.asyncio
async def test_delete_dat_happy_path(isolated_dat_store):
    """Deleting an existing DAT returns success and removes it from the store."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    imported = await dat_routes.import_dat(file=upload)
    dat_id = imported["id"]

    result = await dat_routes.delete_dat(dat_id=dat_id)
    assert result["deleted"] is True
    assert result["id"] == dat_id
    assert isolated_dat_store.list_dats() == []


@pytest.mark.asyncio
async def test_delete_dat_not_found(isolated_dat_store):
    """Deleting a nonexistent DAT raises 404."""
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.delete_dat(dat_id="doesnotexist")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# /dat/match  (single file)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_file_access_denied(tmp_path, isolated_dat_store, monkeypatch):
    """match_file raises 403 for paths outside configured volumes."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: False)
    request = dat_routes.MatchRequest(path="/outside/file.iso")
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_file_not_found(tmp_path, isolated_dat_store, monkeypatch):
    """match_file raises 404 when the file does not exist on disk."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    request = dat_routes.MatchRequest(path=str(tmp_path / "nonexistent.iso"))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_match_file_hit(tmp_path, isolated_dat_store, monkeypatch):
    """match_file returns matched=True when file SHA1 is in the DAT index."""
    # Import the sample DAT
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Create a file whose SHA1 matches the DAT entry
    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    iso = tmp_path / "test.iso"
    iso.write_bytes(b"fake content")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is True
    assert result["game_name"] == "Test Game"
    assert result["match_type"] == "file_sha1"


@pytest.mark.asyncio
async def test_match_file_miss(tmp_path, isolated_dat_store, monkeypatch):
    """match_file returns matched=False when SHA1 is not in any DAT."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "unknown.iso"
    iso.write_bytes(b"unknown content")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(
        dat_routes, "compute_file_sha1",
        AsyncMock(return_value="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
    )

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is False


# ---------------------------------------------------------------------------
# /dat/match-batch  (cache + invalidation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_batch_no_dats(isolated_dat_store, monkeypatch):
    """match-batch returns unmatched for all paths when no DATs are loaded."""
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    request = dat_routes.MatchBatchRequest(paths=["/foo/a.iso", "/foo/b.iso"])
    result = await dat_routes.match_batch(request)

    for path in ["/foo/a.iso", "/foo/b.iso"]:
        assert result["results"][path]["matched"] is False


@pytest.mark.asyncio
async def test_match_batch_uses_cache(tmp_path, isolated_dat_store, monkeypatch):
    """match-batch returns cached results and skips recomputing them."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "cached.iso"
    iso.write_bytes(b"x")
    # Use the normalized path as both cache key and request path
    path = os.path.normpath(os.path.abspath(str(iso)))

    # Seed the cache manually with the normalized path
    cached_result = {"path": path, "matched": True, "game_name": "Cached Game"}
    await isolated_dat_store.set_match(path, cached_result)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    compute_mock = AsyncMock(return_value="deadbeef" * 5)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", compute_mock)

    request = dat_routes.MatchBatchRequest(paths=[path])
    result = await dat_routes.match_batch(request)

    # SHA1 computation should NOT have been called — result came from cache
    compute_mock.assert_not_called()
    assert result["results"][path]["matched"] is True
    assert result["results"][path]["game_name"] == "Cached Game"


@pytest.mark.asyncio
async def test_match_batch_cache_cleared_after_new_import(tmp_path, isolated_dat_store, monkeypatch):
    """Stale 'unmatched' cache entries are cleared when a new DAT is imported."""
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"content")
    path = str(iso)

    # Import first DAT and cache "unmatched" for the file
    upload1 = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload1)
    await isolated_dat_store.set_match(path, {"path": path, "matched": False})

    assert isolated_dat_store.get_match(path) is not None

    # Import a second DAT — this must clear the match cache
    upload2 = _make_upload_file(SECOND_DAT_XML)
    await dat_routes.import_dat(file=upload2)

    # The stale cache entry should be gone
    assert isolated_dat_store.get_match(path) is None

    # match-batch should now recompute (not serve the stale miss)
    target_sha1 = "1122334455667788990011223344556677889900"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchBatchRequest(paths=[path])
    result = await dat_routes.match_batch(request)

    assert result["results"][path]["matched"] is True
    assert result["results"][path]["game_name"] == "Another Game"


# ---------------------------------------------------------------------------
# DATStore.get_dat_name  (O(1) lookup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_dat_name_returns_name(isolated_dat_store):
    """get_dat_name returns the correct name for a known DAT ID."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    imported = await dat_routes.import_dat(file=upload)
    dat_id = imported["id"]

    assert isolated_dat_store.get_dat_name(dat_id) == "Test Redump DAT"


@pytest.mark.asyncio
async def test_get_dat_name_unknown(isolated_dat_store):
    """get_dat_name returns 'Unknown' for an unrecognized DAT ID."""
    assert isolated_dat_store.get_dat_name("nonexistent-id") == "Unknown"


# ---------------------------------------------------------------------------
# _try_chd_header_match  (CHD metadata-backed SHA1 matching)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chd_header_match_hit(tmp_path, isolated_dat_store, monkeypatch):
    """CHD files are matched via their cached header SHA1."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")
    chd_path = str(chd)

    # Mock chd_metadata_store.get_metadata to return sha1 matching the DAT entry
    mock_metadata_store = MagicMock()
    mock_metadata_store.get_metadata = AsyncMock(
        return_value={"sha1": "aabbccddaabbccddaabbccddaabbccddaabbccdd", "data_sha1": ""}
    )
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    # Patch inside the function's module-level import
    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await dat_routes._try_chd_header_match(chd_path)

    assert result is not None
    assert result["matched"] is True
    assert result["match_type"] == "chd_sha1"
    assert result["game_name"] == "Test Game"


@pytest.mark.asyncio
async def test_chd_header_match_no_metadata(tmp_path, isolated_dat_store):
    """_try_chd_header_match returns None when no cached metadata exists."""
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"fake chd")

    mock_metadata_store = MagicMock()
    mock_metadata_store.get_metadata = AsyncMock(return_value=None)

    with patch("services.chd_metadata_store.chd_metadata_store", mock_metadata_store):
        result = await dat_routes._try_chd_header_match(str(chd))

    assert result is None


# ---------------------------------------------------------------------------
# Path normalization in match_batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_batch_normalizes_paths(tmp_path, isolated_dat_store, monkeypatch):
    """match-batch normalizes paths before cache lookup and file checks."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x")
    normal_path = os.path.normpath(os.path.abspath(str(iso)))
    # Provide a non-normalized variant (double slash)
    unnorm_path = str(iso).replace(str(tmp_path), str(tmp_path) + "/")

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchBatchRequest(paths=[unnorm_path])
    result = await dat_routes.match_batch(request)

    # Result should be accessible via the original (input) key
    assert result["results"][unnorm_path]["matched"] is True
    # The path field in the result should be the normalized form
    assert result["results"][unnorm_path]["path"] == normal_path


@pytest.mark.asyncio
async def test_match_batch_duplicate_normalized_paths(tmp_path, isolated_dat_store, monkeypatch):
    """Two input paths that normalize to the same file share a single result."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"x")
    normal_path = os.path.normpath(os.path.abspath(str(iso)))
    # Two spelling variants that normalize to the same path
    variant_a = normal_path
    variant_b = str(iso).replace(str(tmp_path), str(tmp_path) + "/")

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    compute_mock = AsyncMock(return_value=target_sha1)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", compute_mock)

    request = dat_routes.MatchBatchRequest(paths=[variant_a, variant_b])
    result = await dat_routes.match_batch(request)

    # Both original keys should appear in results
    assert result["results"][variant_a]["matched"] is True
    assert result["results"][variant_b]["matched"] is True
    # SHA1 should only have been computed once (shared result)
    assert compute_mock.call_count == 1


# ---------------------------------------------------------------------------
# Security: symlink-based path traversal and symlink loops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_file_symlink_escaping_volume_denied(tmp_path, monkeypatch):
    """Symlink inside a configured volume that resolves outside the volume is rejected with 403."""
    from config import settings as app_settings

    volume = tmp_path / "volume"
    volume.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.iso"
    target.write_bytes(b"secret content")

    # Symlink inside the volume that points to a file outside the volume
    link = volume / "link.iso"
    link.symlink_to(target)

    # Limit configured volumes to only `volume`; the resolved target lies outside it
    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchRequest(path=str(link))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_file_symlink_loop_returns_4xx_not_500(tmp_path, monkeypatch):
    """A symlink loop is rejected with a 4xx status, not an unhandled 500."""
    from config import settings as app_settings

    volume = tmp_path / "volume"
    volume.mkdir()

    # Create a self-referential symlink loop inside the volume
    loop = volume / "loop.iso"
    loop.symlink_to(loop)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchRequest(path=str(loop))
    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.match_file(request)
    # os.path.realpath returns the unresolved symlink path for a loop (handles
    # ELOOP without raising). is_within_configured_volumes then calls
    # _resolve_path → Path.resolve() raises RuntimeError → caught → returns
    # None → volume check returns False → 403 (not an unhandled 500).
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_match_batch_symlink_escaping_volume_denied(tmp_path, isolated_dat_store, monkeypatch):
    """Batch: symlink inside volume resolving outside is denied per-path with an error."""
    from config import settings as app_settings

    # Load a DAT so the batch endpoint proceeds past the early-exit check
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    volume = tmp_path / "volume"
    volume.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.iso"
    target.write_bytes(b"secret content")

    link = volume / "link.iso"
    link.symlink_to(target)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchBatchRequest(paths=[str(link)])
    result = await dat_routes.match_batch(request)

    path_result = result["results"][str(link)]
    assert path_result["matched"] is False
    assert "access denied" in path_result.get("error", "").lower()


@pytest.mark.asyncio
async def test_match_batch_symlink_loop_returns_denied_not_500(tmp_path, isolated_dat_store, monkeypatch):
    """Batch: a symlink loop path is denied per-path, not an unhandled 500."""
    from config import settings as app_settings

    # Load a DAT so the batch endpoint proceeds past the early-exit check
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    volume = tmp_path / "volume"
    volume.mkdir()

    loop = volume / "loop.iso"
    loop.symlink_to(loop)

    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    request = dat_routes.MatchBatchRequest(paths=[str(loop)])
    result = await dat_routes.match_batch(request)

    # Must not raise; the symlink loop is caught by is_within_configured_volumes:
    # os.path.realpath returns the unresolved symlink path (handles ELOOP
    # gracefully), then is_within_configured_volumes calls _resolve_path →
    # Path.resolve() raises RuntimeError for the loop → caught → returns None
    # → volume check returns False → access denied.
    assert str(loop) in result["results"]
    path_result = result["results"][str(loop)]
    assert path_result["matched"] is False
    assert "access denied" in path_result.get("error", "").lower()


# ---------------------------------------------------------------------------
# /dat/sync  /dat/sync/status  /dat/sync/cancel
# ---------------------------------------------------------------------------

def _make_mock_request():
    """Return a minimal mock HTTP Request with app.state.background_tasks."""
    mock_req = MagicMock()
    mock_req.app.state.background_tasks = set()
    return mock_req


@pytest.mark.asyncio
async def test_sync_mameredump_starts_sync(monkeypatch):
    """POST /dat/sync starts a background sync and returns status=started."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False
    mock_svc.sync = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert result["status"] == "started"


@pytest.mark.asyncio
async def test_sync_mameredump_409_on_race_condition(monkeypatch):
    """POST /dat/sync returns 409 when the background task detects a concurrent sync."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False  # passes early check
    mock_svc.sync = AsyncMock(side_effect=RuntimeError("Sync already in progress"))
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert exc_info.value.status_code == 409

@pytest.mark.asyncio
async def test_sync_mameredump_409_when_already_syncing(monkeypatch):
    """POST /dat/sync returns 409 when a sync is already in progress."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = True
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_mameredump(http_request=_make_mock_request(), request=None)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_status_returns_shape(monkeypatch):
    """GET /dat/sync/status returns a dict with expected fields."""
    mock_svc = MagicMock()
    mock_svc.get_status = MagicMock(return_value={
        "syncing": False,
        "progress": {"status": "complete", "files_imported": 3},
        "last_sync_tag": "0.285",
        "last_sync_at": "2026-04-01T00:00:00Z",
        "last_sync_files": 3,
        "error": "",
    })
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_status()
    assert result["syncing"] is False
    assert result["last_sync_tag"] == "0.285"
    assert result["progress"]["files_imported"] == 3


@pytest.mark.asyncio
async def test_sync_cancel_success(monkeypatch):
    """POST /dat/sync/cancel returns status=cancelling when sync is running."""
    mock_svc = MagicMock()
    mock_svc.cancel = MagicMock(return_value=True)
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    result = await dat_routes.sync_cancel()
    assert result["status"] == "cancelling"


@pytest.mark.asyncio
async def test_sync_cancel_409_when_not_syncing(monkeypatch):
    """POST /dat/sync/cancel returns 409 when no sync is in progress."""
    mock_svc = MagicMock()
    mock_svc.cancel = MagicMock(return_value=False)
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    with pytest.raises(HTTPException) as exc_info:
        await dat_routes.sync_cancel()
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_mameredump_passes_tag(monkeypatch):
    """POST /dat/sync forwards the tag from the request body."""
    mock_svc = MagicMock()
    mock_svc.is_syncing = False
    mock_svc.sync = AsyncMock(return_value={"status": "complete"})
    monkeypatch.setattr(dat_routes, "_get_sync_service", lambda: mock_svc)

    await dat_routes.sync_mameredump(
        http_request=_make_mock_request(),
        request=dat_routes.SyncRequest(tag="0.285"),
    )
    # Give the background task a chance to start
    import asyncio
    await asyncio.sleep(0)
    mock_svc.sync.assert_called_once_with(tag="0.285")


# ---------------------------------------------------------------------------
# Match concurrency + size cap (Deliverable 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_skips_oversized_file(tmp_path, isolated_dat_store, monkeypatch):
    """When MATCH_MAX_FILE_SIZE is set, files larger than the cap are
    reported as unmatched *without* running the SHA1 hasher."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    huge = tmp_path / "huge.iso"
    huge.write_bytes(b"x" * 1024)

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    # Tiny cap (10 bytes) guarantees the 1 KB file trips it.
    monkeypatch.setattr(dat_routes.settings, "match_max_file_size", 10)

    hash_mock = AsyncMock(return_value="dead" * 10)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", hash_mock)

    request = dat_routes.MatchRequest(path=str(huge))
    result = await dat_routes.match_file(request)

    assert result["matched"] is False
    assert result.get("reason") == "file too large"
    # Critical: the hasher must NOT have been invoked.
    hash_mock.assert_not_called()


@pytest.mark.asyncio
async def test_match_respects_concurrency_cap(tmp_path, isolated_dat_store, monkeypatch):
    """With MAX_MATCH_CONCURRENCY=1, simultaneous match_file calls
    execute compute_file_sha1 serially (never two at once)."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    # Rebuild the workload_limiter with match_limit=1 for this test.
    # (Default is already 1, but we set it explicitly so the test is
    # self-documenting and robust to future default changes.)
    # Use __class__ to reuse the exact class already loaded by dat_routes,
    # avoiding any duplicate module-singleton risk from a separate import.
    WorkloadLimiter = dat_routes.workload_limiter.__class__
    new_limiter = WorkloadLimiter(
        verify_limit=1, metadata_scan_limit=1, match_limit=1,
    )
    monkeypatch.setattr(dat_routes, "workload_limiter", new_limiter)

    active = 0
    peak = 0
    barrier = asyncio.Event()

    async def _fake_hash(path):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Wait until all callers are in-flight before returning, so
            # if the limiter is NOT serialising we'd observe peak > 1.
            await asyncio.wait_for(barrier.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            # Expected: under serial execution, only one caller ever
            # enters this function at a time, so the barrier is never
            # set by anyone else.
            pass
        active -= 1
        return "a" * 40  # a sha1 that is NOT in the loaded DAT

    monkeypatch.setattr(dat_routes, "compute_file_sha1", _fake_hash)
    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)

    paths = []
    for i in range(5):
        iso_path = tmp_path / f"f{i}.iso"
        iso_path.write_bytes(b"x")
        paths.append(iso_path)

    requests = [
        dat_routes.match_file(dat_routes.MatchRequest(path=str(path)))
        for path in paths
    ]
    await asyncio.gather(*requests)

    # Under match_limit=1 the hasher runs strictly one at a time.
    assert peak == 1, f"concurrency cap breached: peak={peak}"


@pytest.mark.asyncio
async def test_match_file_hit_respects_size_cap_off(tmp_path, isolated_dat_store, monkeypatch):
    """With MATCH_MAX_FILE_SIZE=0 (disabled), any file is hashed and matched."""
    upload = _make_upload_file(SAMPLE_DAT_XML)
    await dat_routes.import_dat(file=upload)

    target_sha1 = "aabbccddaabbccddaabbccddaabbccddaabbccdd"
    iso = tmp_path / "any-size.iso"
    iso.write_bytes(b"whatever")

    monkeypatch.setattr(dat_routes, "is_within_configured_volumes", lambda p: True)
    monkeypatch.setattr(dat_routes.settings, "match_max_file_size", 0)
    monkeypatch.setattr(dat_routes, "compute_file_sha1", AsyncMock(return_value=target_sha1))

    request = dat_routes.MatchRequest(path=str(iso))
    result = await dat_routes.match_file(request)

    assert result["matched"] is True
