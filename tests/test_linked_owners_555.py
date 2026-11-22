"""A sheet, playlist or set in another folder that reaches a file through a
link still owns it (#555).

A ``.cue`` names tracks in its folder and below, and through a folder link
inside the Volume it reaches another folder: ``/vol/other/A.cue`` naming
``link/Shared.bin`` with ``link`` pointing at ``/vol/tracks``. The walk up
from ``/vol/tracks/Shared.bin`` never read ``/vol/other``, so deleting the
track broke that sheet. Every delete and rename now also reads the folders
whose links reach the file, and keeps the file when that can't be told.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.routes import files as files_routes


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    from config import settings

    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))

    async def noop(*_a, **_k):
        return None

    for store in (files_routes.verification_store, files_routes.chd_metadata_store):
        monkeypatch.setattr(store, "clear", noop)
        monkeypatch.setattr(store, "move", noop)
    return volume


def _write(path: Path, data: bytes = b"\0" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _delete_ok():
    return SimpleNamespace(headers=Headers({"x-chd-action-confirm": "delete-file"}))


def _rename_ok():
    return SimpleNamespace(headers=Headers({"x-chd-action-confirm": "rename-file"}))


def _sheet_through_a_folder_link(vol: Path) -> tuple[Path, Path]:
    """The #555 repro: other/A.cue names link/Shared.bin, link -> tracks."""
    track = _write(vol / "tracks" / "Shared.bin")
    (vol / "other").mkdir()
    os.symlink(vol / "tracks", vol / "other" / "link")
    sheet = _write(
        vol / "other" / "A.cue", b'FILE "link/Shared.bin" BINARY\n  TRACK 01 MODE1/2352\n',
    )
    return sheet, track


def test_the_owner_question_sees_a_sheet_through_a_folder_link(vol):
    from services import image_sets

    sheet, track = _sheet_through_a_folder_link(vol)

    owners = image_sets.owners_naming(str(track), stop_at=str(vol))

    assert [os.path.realpath(o) for o in owners] == [os.path.realpath(sheet)]


@pytest.mark.asyncio
async def test_a_batch_keeps_a_track_a_sheet_reaches_through_a_folder_link(vol):
    sheet, track = _sheet_through_a_folder_link(vol)

    response = await files_routes.delete_files_batch(
        _delete_ok(), files_routes.BulkDeleteRequest(paths=[str(track)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert sheet.name in result["error"]
    assert track.exists()


@pytest.mark.asyncio
async def test_a_batch_takes_the_track_with_the_sheet_that_reaches_it(vol):
    sheet, track = _sheet_through_a_folder_link(vol)

    response = await files_routes.delete_files_batch(
        _delete_ok(), files_routes.BulkDeleteRequest(paths=[str(track), str(sheet)]),
    )

    assert response["failed"] == 0, response
    assert not track.exists() and not sheet.exists()


@pytest.mark.asyncio
async def test_a_single_delete_keeps_a_disc_a_playlist_reaches_through_a_link(vol):
    disc = _write(vol / "discs" / "Game (Disc 1).chd")
    (vol / "lists").mkdir()
    os.symlink(vol / "discs", vol / "lists" / "d")
    _write(vol / "lists" / "Game.m3u", b"d/Game (Disc 1).chd\n")

    with pytest.raises(HTTPException) as err:
        await files_routes.delete_file(_delete_ok(), path=str(disc), recursive=False)

    assert err.value.status_code == 409
    assert "Game.m3u" in err.value.detail
    assert disc.exists()


@pytest.mark.asyncio
async def test_renaming_a_set_part_another_folder_links_to_is_refused(vol):
    """other/Game.iso.0's set reads other/Game.iso.1, a link to parts/Game.iso.1."""
    part = _write(vol / "parts" / "Game.iso.1")
    _write(vol / "other" / "Game.iso.0")
    os.symlink(part, vol / "other" / "Game.iso.1")

    with pytest.raises(HTTPException) as err:
        await files_routes.rename_file(_rename_ok(), path=str(part), new_name="Moved.iso.1")

    assert err.value.status_code == 409
    assert part.exists()


def test_a_delete_on_verify_plan_keeps_a_track_a_linked_sheet_names(vol):
    from utils.delete_plan import build_delete_plan, collect_delete_paths

    _sheet, _track = _sheet_through_a_folder_link(vol)
    own = _write(vol / "tracks" / "Own.cue", b'FILE "Shared.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    plan = build_delete_plan(str(own), confine_to_volumes=True)

    assert any("A.cue" in error for error in plan["errors"]), plan
    # A plan with an error deletes nothing at all.
    with pytest.raises(ValueError, match="A.cue"):
        collect_delete_paths(str(own))


@pytest.mark.asyncio
async def test_a_folder_link_out_of_the_volume_leaves_ownership_unknown(vol, tmp_path):
    """A link to a folder outside may lead back in; what it reaches is never
    walked, so the file is kept."""
    track = _write(vol / "tracks" / "Shared.bin")
    outside = tmp_path / "outside"
    outside.mkdir()
    (vol / "other").mkdir()
    os.symlink(outside, vol / "other" / "away")

    response = await files_routes.delete_files_batch(
        _delete_ok(), files_routes.BulkDeleteRequest(paths=[str(track)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert "outside the Volume" in result["error"]
    assert track.exists()


@pytest.mark.asyncio
async def test_a_file_no_link_reaches_still_goes(vol):
    _sheet_through_a_folder_link(vol)
    loose = _write(vol / "loose" / "Loose.iso")

    result = await files_routes.delete_file(_delete_ok(), path=str(loose), recursive=False)

    assert result["success"] is True
    assert not loose.exists()


@pytest.mark.asyncio
async def test_a_slow_folder_that_keeps_listing_is_never_cut_off(vol, monkeypatch):
    """Codex 4658820242: the links walk reports every entry as it is read, so
    a big or slow folder that keeps answering outlasts the stall bound."""
    import time

    from routes import volume_guard
    from services import image_sets

    for n in range(8):
        _write(vol / "big" / f"Game {n}.iso")
    real_scandir = os.scandir

    class Slow:
        """os.scandir that reads one entry every 0.1 s."""

        def __init__(self, path):
            self.inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                time.sleep(0.1)
                yield entry

    monkeypatch.setattr(image_sets.os, "scandir", Slow)
    monkeypatch.setattr(volume_guard, "WALK_STALL_SECONDS", 0.35)

    links_of = await files_routes.volume_links_of([str(vol / "big" / "Game 0.iso")])

    assert list(links_of.values()) == [[]], links_of
