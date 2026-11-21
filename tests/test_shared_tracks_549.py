"""A track two sheets share is never deleted out from under one of them
(#549 review, Codex 4654246812).

Two listed ``.cue``/``.gdi`` sheets can name the same track. The listing then
folds it under neither (it stays its own row), deleting a batch refuses a
file that a sheet outside the batch still names, and a delete-on-verify plan
refuses to take a track that another sheet names.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.routes import files as files_routes


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    return tmp_path


def _write(path: Path, data: bytes = b"\0" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _two_sheets_one_track(folder: Path) -> tuple[Path, Path, Path, Path]:
    shared = _write(folder / "Shared.bin")
    own = _write(folder / "A (Track 2).bin")
    a = _write(folder / "A.cue", (
        b'FILE "Shared.bin" BINARY\n  TRACK 01 MODE2/2352\n'
        b'FILE "A (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n'
    ))
    b = _write(folder / "B.cue", b'FILE "Shared.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    return a, b, shared, own


@pytest.mark.asyncio
async def test_a_shared_track_is_folded_under_no_sheet(vol):
    a, _b, _shared, _own = _two_sheets_one_track(vol / "psx")

    listing = await files_routes.list_files(
        path=str(vol / "psx"), show_archives=True, summarize_archives=False,
    )
    marks = {e.name: e.companion_of for e in listing.entries}

    assert marks["Shared.bin"] is None
    assert marks["A (Track 2).bin"] == str(a)


def _confirmed():
    return SimpleNamespace(headers=Headers({"x-chd-action-confirm": "delete-file"}))


@pytest.mark.asyncio
async def test_a_batch_never_deletes_a_track_another_sheet_still_names(vol):
    a, b, shared, own = _two_sheets_one_track(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(a), str(own), str(shared)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(shared)]["success"] is False
    assert "B.cue" in by_path[str(shared)]["error"]
    assert shared.exists() and b.exists()
    assert not a.exists() and not own.exists()


@pytest.mark.asyncio
async def test_a_batch_deletes_a_shared_track_with_every_sheet_that_names_it(vol):
    a, b, shared, own = _two_sheets_one_track(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(),
        files_routes.BulkDeleteRequest(paths=[str(a), str(b), str(own), str(shared)]),
    )

    assert response["failed"] == 0, response
    assert not any(p.exists() for p in (a, b, own, shared))


def test_a_delete_on_verify_plan_keeps_a_track_another_sheet_names(vol):
    from utils.delete_plan import build_delete_plan

    a, _b, _shared, _own = _two_sheets_one_track(vol / "psx")

    plan = build_delete_plan(str(a), confine_to_volumes=True)

    assert any("B.cue" in error for error in plan["errors"]), plan


# --- Every kind of companion reference (Codex 4654538677) ---------------------

def _two_playlists_one_disc(folder: Path) -> tuple[Path, Path, Path]:
    disc = _write(folder / "Game (Disc 1).chd")
    a = _write(folder / "A.m3u", b"Game (Disc 1).chd\n")
    b = _write(folder / "B.m3u", b"Game (Disc 1).chd\n")
    return a, b, disc


@pytest.mark.asyncio
async def test_a_disc_two_playlists_name_is_folded_under_neither(vol):
    _two_playlists_one_disc(vol / "psx")

    listing = await files_routes.list_files(
        path=str(vol / "psx"), show_archives=True, summarize_archives=False,
    )

    assert {e.name: e.disc_of for e in listing.entries}["Game (Disc 1).chd"] is None


@pytest.mark.asyncio
async def test_a_batch_keeps_a_disc_another_playlist_still_names(vol):
    a, b, disc = _two_playlists_one_disc(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(a), str(disc)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(disc)]["success"] is False
    assert "B.m3u" in by_path[str(disc)]["error"]
    assert disc.exists() and b.exists() and not a.exists()


@pytest.mark.asyncio
async def test_a_batch_deletes_a_disc_with_every_playlist_that_names_it(vol, monkeypatch):
    async def forget(_path):
        return None

    monkeypatch.setattr(files_routes.verification_store, "clear", forget)
    monkeypatch.setattr(files_routes.chd_metadata_store, "clear", forget)
    a, b, disc = _two_playlists_one_disc(vol / "psx")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(a), str(b), str(disc)]),
    )

    assert response["failed"] == 0, response
    assert not any(p.exists() for p in (a, b, disc))


@pytest.mark.asyncio
async def test_a_batch_keeps_a_later_rar_volume_without_its_first(vol):
    from .rar_fixtures import RAR5_SET, copy_set

    first, second, _third = copy_set(RAR5_SET, vol / "usenet")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(second)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert first.name in result["error"]
    assert second.exists()


@pytest.mark.asyncio
async def test_an_unreadable_playlist_keeps_what_it_might_name(vol):
    folder = vol / "psx"
    disc = _write(folder / "Game.chd")
    playlist = _write(folder / "Other.m3u", b"Game.chd\n")
    playlist.chmod(0)
    try:
        response = await files_routes.delete_files_batch(
            _confirmed(), files_routes.BulkDeleteRequest(paths=[str(disc)]),
        )
    finally:
        playlist.chmod(0o644)

    assert response["results"][0]["success"] is False
    assert disc.exists()


def test_a_delete_on_verify_plan_keeps_a_companion_another_owner_names(vol):
    """The plan's own helper, for a track named by a playlist too."""
    from utils.delete_plan import build_delete_plan

    folder = vol / "psx"
    _write(folder / "Game.bin")
    cue = _write(folder / "Game.cue", b'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    _write(folder / "Odd.m3u", b"Game.bin\n")

    plan = build_delete_plan(str(cue), confine_to_volumes=True)

    assert any("Odd.m3u" in error for error in plan["errors"]), plan
