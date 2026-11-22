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
from fastapi import HTTPException
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


@pytest.mark.asyncio
async def test_a_track_stays_when_its_requested_sheet_fails_to_delete(vol, monkeypatch):
    """CodeRabbit 4654557559: a sheet in the request that isn't actually
    deleted still needs its tracks, whatever order the request lists them in."""
    folder = vol / "psx"
    track = _write(folder / "Game.bin")
    cue = _write(folder / "Game.cue", b'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    real_remove = files_routes.os.remove

    def refuse_the_sheet(path):
        if str(path).endswith(".cue"):
            raise PermissionError("read-only")
        real_remove(path)

    monkeypatch.setattr(files_routes.os, "remove", refuse_the_sheet)

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(track), str(cue)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(cue)]["success"] is False
    assert by_path[str(track)]["success"] is False
    assert track.exists() and cue.exists()


@pytest.mark.asyncio
async def test_a_track_listed_before_its_sheet_is_deleted_with_it(vol):
    folder = vol / "psx"
    track = _write(folder / "Game.bin")
    cue = _write(folder / "Game.cue", b'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n')

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(track), str(cue)]),
    )

    assert response["failed"] == 0, response
    assert not track.exists() and not cue.exists()


# --- A set goes whole or not at all (Codex 4654710759) ------------------------

def _split_disc_in_a_playlist(folder: Path) -> tuple[Path, Path, Path]:
    first = _write(folder / "Game.iso.0")
    second = _write(folder / "Game.iso.1")
    playlist = _write(folder / "Game.m3u", b"Game.iso.0\n")
    return playlist, first, second


@pytest.mark.asyncio
async def test_a_batch_never_deletes_part_of_a_split_set(vol):
    playlist, first, second = _split_disc_in_a_playlist(vol / "ps3")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(playlist), str(first)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(first)]["success"] is False
    assert "Game.iso.1" in by_path[str(first)]["error"]
    assert first.exists() and second.exists()


@pytest.mark.asyncio
async def test_a_batch_deletes_a_whole_split_set(vol, monkeypatch):
    async def forget(_path):
        return None

    monkeypatch.setattr(files_routes.verification_store, "clear", forget)
    playlist, first, second = _split_disc_in_a_playlist(vol / "ps3")

    response = await files_routes.delete_files_batch(
        _confirmed(),
        files_routes.BulkDeleteRequest(paths=[str(second), str(first), str(playlist)]),
    )

    assert response["failed"] == 0, response
    assert not any(p.exists() for p in (playlist, first, second))


def test_a_delete_on_verify_plan_takes_a_split_set_whole(vol):
    from utils.delete_plan import build_delete_plan

    _playlist, first, second = _split_disc_in_a_playlist(vol / "ps3")

    plan = build_delete_plan(str(first), confine_to_volumes=True)

    assert str(second.resolve()) in plan["delete_paths"], plan


@pytest.mark.asyncio
async def test_a_batch_keeps_a_later_split_part_without_its_first(vol):
    _playlist, first, second = _split_disc_in_a_playlist(vol / "ps3")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(second)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert first.name in result["error"]
    assert first.exists() and second.exists()


@pytest.mark.asyncio
async def test_a_batch_keeps_an_old_style_rar_volume_without_its_first(vol):
    from .rar_fixtures import RAR4_SET, copy_set

    first, r00, _r01 = copy_set(RAR4_SET, vol / "usenet")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(r00)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert first.name in result["error"]
    assert r00.exists()


# --- A UTF-16 playlist (Codex 4654858505) ------------------------------------

def _utf16(text: str) -> bytes:
    return "\ufeff".encode("utf-16-le") + text.encode("utf-16-le")


@pytest.mark.asyncio
async def test_a_utf16_playlist_beside_an_unrelated_file_never_fails_the_batch(vol, monkeypatch):
    async def forget(_path):
        return None

    monkeypatch.setattr(files_routes.verification_store, "clear", forget)
    folder = vol / "psx"
    _write(folder / "Game (Disc 1).chd")
    _write(folder / "Game.m3u", _utf16("Game (Disc 1).chd\r\n"))
    other = _write(folder / "Other.iso")

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(other)]),
    )

    assert response["results"][0]["success"] is True, response
    assert not other.exists()


@pytest.mark.asyncio
async def test_a_utf16_playlist_still_owns_its_disc(vol):
    folder = vol / "psx"
    disc = _write(folder / "Game (Disc 1).chd")
    _write(folder / "Game.m3u", _utf16("Game (Disc 1).chd\r\n"))

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(disc)]),
    )

    assert response["results"][0]["success"] is False
    assert "Game.m3u" in response["results"][0]["error"]
    assert disc.exists()


@pytest.mark.asyncio
async def test_a_utf16_playlist_folds_its_discs(vol):
    folder = vol / "psx"
    disc = _write(folder / "Game (Disc 1).chd")
    playlist = _write(folder / "Game.m3u", _utf16("Game (Disc 1).chd\r\n"))

    listing = await files_routes.list_files(
        path=str(folder), show_archives=True, summarize_archives=False,
    )

    assert {e.name: e.disc_of for e in listing.entries}[disc.name] == str(playlist)


def test_a_path_that_cannot_be_stated_is_unknown_ownership(tmp_path):
    from services import image_sets

    own = tmp_path / "a.bin"
    own.write_bytes(b"x")
    with pytest.raises(image_sets.SheetOwnerUnknown):
        image_sets._is_track(own.stat(), str(own), str(tmp_path / "bad\x00name"))


# --- CodeRabbit round 3 ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_split_part_that_cannot_be_probed_still_keeps_the_set(vol, monkeypatch):
    """CodeRabbit 4654883856: the tool's probe reads a part it can't stat as
    absent; the set is still told by name, so .0 alone is kept."""
    from services.tools import registry

    first = _write(vol / "ps3" / "Game.iso.0")
    second = _write(vol / "ps3" / "Game.iso.1")
    monkeypatch.setattr(registry, "split_members", lambda _path: [])

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(first)]),
    )

    [result] = response["results"]
    assert result["success"] is False
    assert "Game.iso.1" in result["error"]
    assert first.exists() and second.exists()


@pytest.mark.asyncio
async def test_removing_a_link_to_a_sheet_never_frees_its_tracks(vol):
    """CodeRabbit 4654883868: deleting a link to A.cue removes the link, not
    the sheet, so the sheet's track stays."""
    folder = vol / "psx"
    track = _write(folder / "Track.bin")
    cue = _write(folder / "A.cue", b'FILE "Track.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    link = folder / "Alias.link"
    link.symlink_to(cue)

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(link), str(track)]),
    )

    by_path = {r["path"]: r for r in response["results"]}
    assert by_path[str(track)]["success"] is False
    assert track.exists() and cue.exists()


# --- Containment before any ownership scan (Codex 4655058131) -----------------

@pytest.fixture(name="split_roots")
def _split_roots(tmp_path, monkeypatch):
    """A Volume and a folder beside it that is outside every Volume."""
    from config import settings

    volume = tmp_path / "vol"
    outside = tmp_path / "private"
    volume.mkdir()
    outside.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    monkeypatch.setattr(settings, "data_mount_root", str(volume))
    return volume, outside


@pytest.mark.asyncio
async def test_an_outside_path_is_refused_before_anything_beside_it_is_read(
    split_roots, monkeypatch,
):
    from services import image_sets

    _volume, outside = split_roots
    secret = _write(outside / "Secret.chd")
    _write(outside / "Private collection.m3u", b"Secret.chd\n")
    asked = []
    real = image_sets.owners_naming
    monkeypatch.setattr(
        image_sets, "owners_naming", lambda p, **kw: asked.append(p) or real(p, **kw),
    )

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(secret)]),
    )

    [result] = response["results"]
    assert result == {
        "path": str(secret), "success": False,
        "error": "Access denied: path outside configured volumes",
    }
    assert asked == []
    assert secret.exists()


def test_the_ownership_walk_never_opens_a_sheet_outside_the_volume(split_roots, monkeypatch):
    from services import image_sets
    from utils import sheet_text

    volume, outside = split_roots
    track = _write(volume / "psx" / "Track.bin")
    out_sheet = _write(outside / "Out.cue", b'FILE "Track.bin" BINARY\n')
    (volume / "psx" / "Link.cue").symlink_to(out_sheet)
    opened = []
    monkeypatch.setattr(sheet_text, "sheet_is_readable", lambda p: opened.append(str(p)))
    monkeypatch.setattr(sheet_text, "sheet_track_list", lambda p, **kw: opened.append(str(p)))

    with pytest.raises(image_sets.SheetOwnerUnknown) as err:
        image_sets.owners_naming(str(track), stop_at=str(volume))

    assert "Out.cue" not in str(err.value)
    assert not opened, opened


def test_a_delete_plan_never_scans_outside_the_volumes(split_roots, monkeypatch):
    from services import image_sets
    from utils.delete_plan import build_delete_plan

    _volume, outside = split_roots
    _write(outside / "Track.bin")
    cue = _write(outside / "Game.cue", b'FILE "Track.bin" BINARY\n  TRACK 01 MODE2/2352\n')
    asked = []
    monkeypatch.setattr(image_sets, "owners_naming", lambda p, **kw: asked.append(p) or [])

    plan = build_delete_plan(str(cue), confine_to_volumes=True)

    assert asked == []
    assert plan["unsafe_paths"], plan


# --- An 8-bit playlist in another code page (Codex 4655165263) -----------------

@pytest.mark.asyncio
async def test_a_cp1252_playlist_still_owns_its_disc(vol):
    folder = vol / "psx"
    disc = _write(folder / "Café.chd")
    _write(folder / "B.m3u", "Café.chd\r\n".encode("cp1252"))

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(disc)]),
    )

    assert response["results"][0]["success"] is False
    assert "B.m3u" in response["results"][0]["error"]
    assert disc.exists()


@pytest.mark.asyncio
async def test_a_cp1252_playlist_folds_its_discs(vol):
    folder = vol / "psx"
    disc = _write(folder / "Café.chd")
    playlist = _write(folder / "B.m3u", "Café.chd\r\n".encode("cp1252"))

    listing = await files_routes.list_files(
        path=str(folder), show_archives=True, summarize_archives=False,
    )

    assert {e.name: e.disc_of for e in listing.entries}[disc.name] == str(playlist)


def test_every_entry_reads_an_8_bit_playlist_each_way():
    from services.referrers import m3u

    found = m3u.every_entry("Café.chd\n".encode("cp1252"))

    assert "Café.chd" in found


@pytest.mark.asyncio
async def test_a_cp1252_sheet_still_owns_its_track(vol):
    folder = vol / "psx"
    track = _write(folder / "Café.bin")
    sheet = 'FILE "Café.bin" BINARY\r\n  TRACK 01 MODE1/2352\r\n'
    _write(folder / "B.cue", sheet.encode("cp1252"))

    response = await files_routes.delete_files_batch(
        _confirmed(), files_routes.BulkDeleteRequest(paths=[str(track)]),
    )

    assert response["results"][0]["success"] is False
    assert "B.cue" in response["results"][0]["error"]
    assert track.exists()


@pytest.mark.parametrize("data", [
    "\ufeffA\u2028B.chd\r\n".encode("utf-16-le"),
    b"A\x85B.chd\r\n",
    b"A\x1cB.chd\n",
])
def test_every_entry_splits_on_cr_and_lf_only(data):
    """CodeRabbit 4655194805: U+2028, NEL (Latin-1 0x85) and the other
    breaks str.splitlines knows are part of a name, not line ends."""
    from services.referrers import m3u

    found = m3u.every_entry(data)

    assert "A" not in found and "B.chd" not in found, found


# --- One file deleted or renamed from its own menu (captain, 8984601e) ---------

def _one():
    return _confirmed()


@pytest.fixture(autouse=True)
def _no_stores(monkeypatch):
    """The path bookkeeping a delete or rename does has no database here."""
    async def noop(*_a, **_k):
        return None

    for store in (files_routes.verification_store, files_routes.chd_metadata_store):
        monkeypatch.setattr(store, "clear", noop)
        monkeypatch.setattr(store, "move", noop)


def _rename_ok():
    return SimpleNamespace(headers=Headers({"x-chd-action-confirm": "rename-file"}))


def _playlist_and_disc(folder: Path) -> Path:
    disc = _write(folder / "Saga (Disc 1).chd")
    _write(folder / "Saga.m3u", b"Saga (Disc 1).chd\n")
    return disc


@pytest.mark.asyncio
async def test_deleting_one_disc_a_playlist_names_keeps_it(vol):
    disc = _playlist_and_disc(vol / "psx")

    with pytest.raises(HTTPException) as err:
        await files_routes.delete_file(_one(), path=str(disc), recursive=False)

    assert err.value.status_code == 409
    assert "Saga.m3u" in err.value.detail
    assert disc.exists()


@pytest.mark.asyncio
async def test_deleting_one_track_its_sheet_names_keeps_it(vol):
    a, _b, shared, own = _two_sheets_one_track(vol / "psx")

    for track in (shared, own):
        with pytest.raises(HTTPException) as err:
            await files_routes.delete_file(_one(), path=str(track), recursive=False)
        assert err.value.status_code == 409, track
        assert track.exists()
    assert a.exists()


@pytest.mark.asyncio
async def test_one_part_of_a_split_set_is_never_deleted_or_renamed_alone(vol):
    """A set goes whole or not at all; the game's row deletes it whole."""
    first = _write(vol / "ps3" / "Game.iso.0")
    second = _write(vol / "ps3" / "Game.iso.1")

    for part in (first, second):
        with pytest.raises(HTTPException) as err:
            await files_routes.delete_file(_one(), path=str(part), recursive=False)
        assert err.value.status_code == 409, part
        with pytest.raises(HTTPException) as err:
            await files_routes.rename_file(_rename_ok(), path=str(part), new_name="X.iso.0")
        assert err.value.status_code == 409, part
    assert first.exists() and second.exists()


@pytest.mark.asyncio
async def test_a_single_delete_keeps_a_file_whose_owners_are_unknown(vol, monkeypatch):
    from services import image_sets

    loose = _write(vol / "psx" / "Loose.iso")

    def unknown(*_a, **_k):
        raise image_sets.SheetOwnerUnknown("X.cue could not be read")

    monkeypatch.setattr(image_sets, "owners_naming", unknown)
    with pytest.raises(HTTPException) as err:
        await files_routes.delete_file(_one(), path=str(loose), recursive=False)

    assert err.value.status_code == 409
    assert "unknown" in err.value.detail
    assert loose.exists()


@pytest.mark.asyncio
async def test_a_single_delete_of_a_loose_file_still_goes(vol):
    loose = _write(vol / "psx" / "Loose.iso")

    result = await files_routes.delete_file(_one(), path=str(loose), recursive=False)

    assert result["success"] is True
    assert not loose.exists()


@pytest.mark.asyncio
async def test_a_single_delete_outside_the_volume_is_refused_before_any_scan(
    split_roots, monkeypatch,
):
    from services import image_sets

    _volume, outside = split_roots
    target = _write(outside / "Out.chd")
    asked = []
    monkeypatch.setattr(image_sets, "owners_naming", lambda p, **kw: asked.append(p) or [])

    with pytest.raises(HTTPException) as err:
        await files_routes.delete_file(_one(), path=str(target), recursive=False)

    assert err.value.status_code == 403
    assert asked == []
    assert target.exists()


@pytest.mark.asyncio
async def test_renaming_a_disc_a_playlist_names_is_refused(vol):
    disc = _playlist_and_disc(vol / "psx")

    with pytest.raises(HTTPException) as err:
        await files_routes.rename_file(_rename_ok(), path=str(disc), new_name="Other.chd")

    assert err.value.status_code == 409
    assert "Saga.m3u" in err.value.detail
    assert disc.exists() and not (disc.parent / "Other.chd").exists()


@pytest.mark.asyncio
async def test_renaming_a_track_its_sheet_names_is_refused(vol):
    _a, _b, _shared, own = _two_sheets_one_track(vol / "psx")

    with pytest.raises(HTTPException) as err:
        await files_routes.rename_file(_rename_ok(), path=str(own), new_name="Other.bin")

    assert err.value.status_code == 409
    assert own.exists()


@pytest.mark.asyncio
async def test_a_sheet_and_a_loose_file_can_still_be_renamed(vol):
    a, _b, _shared, _own = _two_sheets_one_track(vol / "psx")
    loose = _write(vol / "psx" / "Loose.iso")

    await files_routes.rename_file(_rename_ok(), path=str(a), new_name="Renamed.cue")
    await files_routes.rename_file(_rename_ok(), path=str(loose), new_name="Moved.iso")

    assert (vol / "psx" / "Renamed.cue").exists()
    assert (vol / "psx" / "Moved.iso").exists()
