"""Tests for MAME Redump DAT sync service."""

import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.dat_sync import DATSyncService


# ---------------------------------------------------------------------------
# DATSyncService unit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_service(tmp_path):
    """Create a DATSyncService with isolated state (bypasses lazy init)."""
    svc = DATSyncService.__new__(DATSyncService)
    svc._repo = "MetalSlug/MAMERedump"
    svc._state_path = tmp_path / "dat_sync.json"
    svc._explicit_state_path = str(svc._state_path)
    svc._lock = threading.Lock()
    svc._syncing = False
    svc._cancel = False
    svc._progress = {}
    svc._state = {}
    return svc


def test_status_default(sync_service):
    """Default status has no sync in progress and empty fields."""
    status = sync_service.get_status()
    assert status["syncing"] is False
    assert status["last_sync_tag"] == ""
    assert status["last_sync_at"] == ""
    assert status["last_sync_files"] == 0


def test_state_persistence(sync_service):
    """State round-trips through save/load."""
    sync_service._state = {
        "last_sync_tag": "0.285",
        "last_sync_at": "2026-02-12T20:00:00Z",
        "last_sync_files": 69,
    }
    sync_service._save_state()
    assert sync_service._state_path.exists()

    loaded = json.loads(sync_service._state_path.read_text())
    assert loaded["last_sync_tag"] == "0.285"
    assert loaded["last_sync_files"] == 69


def test_state_load_missing_file(sync_service):
    """Loading state when file doesn't exist returns empty dict."""
    state = sync_service._load_state()
    assert state == {}


def test_state_load_corrupt_file(sync_service):
    """Loading corrupt JSON returns empty dict."""
    sync_service._state_path.write_text("not json{{{")
    state = sync_service._load_state()
    assert state == {}


def test_cancel_when_not_syncing(sync_service):
    """cancel() returns False when no sync is in progress."""
    assert sync_service.cancel() is False


def test_cancel_when_syncing(sync_service):
    """cancel() returns True and sets flag when syncing."""
    sync_service._syncing = True
    assert sync_service.cancel() is True
    assert sync_service._cancel is True


def test_github_api_url(sync_service):
    """_github_api_url builds correct URLs."""
    url = sync_service._github_api_url("MAME Redump", ref="0.285")
    assert "repos/MetalSlug/MAMERedump/contents/MAME%20Redump" in url
    assert "ref=0.285" in url


def test_raw_url(sync_service):
    """_raw_url builds correct raw content URLs."""
    url = sync_service._raw_url("MAME Redump/test.dat", ref="0.285")
    assert "raw.githubusercontent.com/MetalSlug/MAMERedump/0.285/MAME%20Redump/test.dat" in url


def test_require_https_rejects_non_https(sync_service):
    """_require_https raises ValueError for non-https schemes."""
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("http://example.com/foo")
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("file:///etc/passwd")
    with pytest.raises(ValueError, match="Only https URLs"):
        sync_service._require_https("ftp://example.com/foo")


def test_require_https_allows_https(sync_service):
    """_require_https does not raise for https URLs."""
    sync_service._require_https("https://api.github.com/repos/foo/bar")
    sync_service._require_https("https://raw.githubusercontent.com/foo/bar/main/test.dat")


def test_list_dat_files_filters_non_dat(sync_service):
    """_list_dat_files only returns .dat files."""
    mock_contents = [
        {"name": "test.dat", "path": "MAME Redump/test.dat", "type": "file", "size": 100},
        {"name": "README.md", "path": "MAME Redump/README.md", "type": "file", "size": 50},
        {"name": "MAME", "path": "MAME Redump/MAME", "type": "dir"},
    ]
    with patch.object(sync_service, "_fetch_json", return_value=mock_contents):
        files = sync_service._list_dat_files("MAME Redump", "main")
    assert len(files) == 1
    assert files[0]["name"] == "test.dat"


# ---------------------------------------------------------------------------
# Async sync tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_already_synced(sync_service):
    """Sync returns early when already synced to the requested tag and DATs are present."""
    sync_service._state = {"last_sync_tag": "0.285"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[{"id": "abc", "name": "Test DAT"}])
    mock_dat_store.delete_dat = AsyncMock()
    with patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")
    assert result["status"] == "already_synced"


@pytest.mark.asyncio
async def test_sync_resyncs_when_dats_empty(sync_service, tmp_path):
    """Sync re-runs when tag matches but DAT store is empty (e.g., after manual deletion)."""
    sync_service._state = {"last_sync_tag": "0.285"}
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock(return_value={"id": "abc", "name": "Test", "file_count": 1, "hashes_added": 1})

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    assert result["files_imported"] == 1


@pytest.mark.asyncio
async def test_sync_prevents_concurrent(sync_service):
    """Sync raises when already in progress."""
    sync_service._syncing = True
    with pytest.raises(RuntimeError, match="already in progress"):
        await sync_service.sync()


@pytest.mark.asyncio
async def test_sync_downloads_and_imports(sync_service, tmp_path):
    """Sync downloads DAT files and imports them into the store."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("""\
<?xml version="1.0"?>
<datafile>
  <header><name>Test</name><version>0.285</version></header>
  <game name="G"><rom name="g.iso" size="1024"
    sha1="aabbccddaabbccddaabbccddaabbccddaabbccdd"/></game>
</datafile>
""")

    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock(return_value={
        "id": "abc123", "name": "Test", "file_count": 1, "hashes_added": 1,
    })

    # _list_dat_files is called once per _DAT_DIRS entry (2 dirs).
    # Return 1 file for first dir, empty for second.
    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync()

    assert result["status"] == "complete"
    assert result["files_imported"] == 1
    assert result["tag"] == "0.285"
    assert sync_service._state["last_sync_tag"] == "0.285"
    mock_dat_store.import_dat.assert_called_once()


@pytest.mark.asyncio
async def test_sync_handles_download_error(sync_service, tmp_path):
    """Sync continues when individual file download/import fails."""
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             # _list_dat_files is called once per _DAT_DIRS entry (2 dirs).
             [
                 {"name": "good.dat", "path": "MAME Redump/good.dat", "size": 100},
                 {"name": "bad.dat", "path": "MAME Redump/bad.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", side_effect=[
             OSError("Network error for good.dat"),
             OSError("Network error for bad.dat"),
         ]), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync()

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 2
    assert result["error"] is not None
    mock_dat_store.import_dat.assert_not_called()
    # All downloads failed so existing DATs must NOT have been deleted.
    mock_dat_store.delete_dat.assert_not_called()


@pytest.mark.asyncio
async def test_sync_defers_deletion_until_all_imports_succeed(sync_service, tmp_path):
    """Existing DATs are only deleted after ALL new files are imported successfully."""
    dat_file = tmp_path / "sample.dat"
    dat_file.write_text("<datafile></datafile>")

    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock(return_value={
        "id": "new-dat", "name": "New DAT", "file_count": 1, "hashes_added": 1,
    })

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete"
    # Old DAT deleted only after all new ones were imported without errors.
    mock_dat_store.delete_dat.assert_called_once_with("old-dat")
    assert sync_service._state.get("last_sync_tag") == "0.285"


@pytest.mark.asyncio
async def test_sync_preserves_existing_dats_on_partial_failure(sync_service, tmp_path):
    """If some imports fail, existing DATs are preserved and last_sync_tag is NOT saved."""
    dat_file = tmp_path / "good.dat"
    dat_file.write_text("<datafile></datafile>")

    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock(side_effect=[
        {"id": "new-dat", "name": "New DAT", "file_count": 1, "hashes_added": 1},
        OSError("Import failed for second file"),
    ])

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [
                 {"name": "good.dat", "path": "MAME Redump/good.dat", "size": 100},
                 {"name": "bad.dat", "path": "MAME Redump/bad.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert result["error"] is not None
    # Partial failure: newly imported "new-dat" must be rolled back, old DAT preserved.
    mock_dat_store.delete_dat.assert_called_once_with("new-dat")
    # last_sync_tag must NOT be saved so next sync can retry the missing file.
    assert "last_sync_tag" not in sync_service._state


@pytest.mark.asyncio
async def test_sync_preserves_existing_dats_when_all_downloads_fail(sync_service, tmp_path):
    """If all downloads fail, existing DATs are preserved (not deleted)."""
    existing_dat = {"id": "old-dat", "name": "Old DAT"}
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[existing_dat])
    mock_dat_store.delete_dat = AsyncMock()
    mock_dat_store.import_dat = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [{"name": "test.dat", "path": "MAME Redump/test.dat", "size": 100}],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", side_effect=OSError("Network failure")), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert result["error"] is not None
    # No successful imports → no rollback needed, existing DATs must survive.
    mock_dat_store.delete_dat.assert_not_called()


@pytest.mark.asyncio
async def test_sync_skips_oversized_files(sync_service, tmp_path):
    """Files whose reported size exceeds _MAX_DAT_SIZE are skipped with an error."""
    from app.services.dat_sync import _MAX_DAT_SIZE

    dat_file = tmp_path / "small.dat"
    dat_file.write_text('<datafile><header><name>Small</name></header></datafile>')
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])
    mock_dat_store.import_dat = AsyncMock(return_value={"id": "small-dat"})
    mock_dat_store.delete_dat = AsyncMock()

    with patch.object(sync_service, "_fetch_latest_tag", return_value="0.285"), \
         patch.object(sync_service, "_list_dat_files", side_effect=[
             [
                 # This file is too large and should be skipped without downloading.
                 {"name": "huge.dat", "path": "MAME Redump/huge.dat", "size": _MAX_DAT_SIZE + 1},
                 # This file is fine and will be imported.
                 {"name": "small.dat", "path": "MAME Redump/small.dat", "size": 100},
             ],
             [],
         ]), \
         patch.object(sync_service, "_download_dat", return_value=str(dat_file)) as mock_download, \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        result = await sync_service.sync(tag="0.285")

    # Oversized file causes a partial-failure result.
    assert result["status"] == "complete_with_errors"
    assert len(result["errors"]) == 1
    assert "huge.dat" in result["errors"][0]
    assert result["error"] is not None
    # _download_dat called only for the non-oversized file.
    mock_download.assert_called_once()
    # small.dat was imported (files_imported=1), but rolled back due to partial failure.
    assert result["files_imported"] == 1
    mock_dat_store.delete_dat.assert_called_once_with("small-dat")


@pytest.mark.asyncio
async def test_sync_sets_error_progress_on_exception(sync_service):
    """sync() sets progress status=error when _do_sync raises."""
    mock_dat_store = MagicMock()
    mock_dat_store.list_dats = MagicMock(return_value=[])

    with patch.object(sync_service, "_fetch_latest_tag", side_effect=OSError("DNS failure")), \
         patch.object(sync_service, "_get_dat_store", return_value=mock_dat_store):
        with pytest.raises(OSError):
            await sync_service.sync()

    assert sync_service._progress["status"] == "error"
    assert "DNS failure" in sync_service._progress["error"]
