"""A multi-file game is one row (restructure 2/6, plan item 5).

The folder listing marks every file that belongs to another listed row: a
sheet's tracks, a split dump's parts and a RAR set's later volumes get
``companion_of`` (the head's path), and a playlist's discs get ``disc_of``
(the ``.m3u``'s path). The Files view folds them under their head. The marks
are a hint: a sheet or playlist that can't be read, or a pass that stalls,
marks nothing and the files stay listed on their own.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_path_is_the_volume(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))


def _write(path: Path, data: bytes = b"\0" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _listing(folder: Path) -> dict[str, tuple[str | None, str | None]]:
    from routes import files as files_routes

    listing = await files_routes.list_files(
        path=str(folder), show_archives=True, summarize_archives=False,
    )
    return {e.name: (e.companion_of, e.disc_of) for e in listing.entries}


def _cue(folder: Path, name: str, tracks: list[str]) -> Path:
    for track in tracks:
        _write(folder / track)
    body = "".join(f'FILE "{t}" BINARY\n  TRACK 0{i + 1} MODE2/2352\n' for i, t in enumerate(tracks))
    return _write(folder / name, body.encode())


@pytest.mark.asyncio
async def test_a_cues_tracks_are_marked(tmp_path):
    folder = tmp_path / "psx"
    cue = _cue(folder, "Game.cue", ["Game (Track 1).bin", "Game (Track 2).bin"])

    marks = await _listing(folder)

    assert marks["Game.cue"] == (None, None)
    assert marks["Game (Track 1).bin"] == (str(cue), None)
    assert marks["Game (Track 2).bin"] == (str(cue), None)


@pytest.mark.asyncio
async def test_a_gdis_tracks_are_marked(tmp_path):
    folder = tmp_path / "dc"
    for track in ("track01.bin", "track02.raw", "track03.bin"):
        _write(folder / track)
    gdi = _write(folder / "Game.gdi", (
        b"3\n1 0 4 2352 track01.bin 0\n2 600 0 2352 track02.raw 0\n"
        b"3 45000 4 2352 track03.bin 0\n"
    ))

    marks = await _listing(folder)

    assert {name: m[0] for name, m in marks.items() if name != "Game.gdi"} == {
        "track01.bin": str(gdi), "track02.raw": str(gdi), "track03.bin": str(gdi),
    }


@pytest.mark.asyncio
async def test_a_loose_bin_with_no_sheet_is_not_marked(tmp_path):
    folder = tmp_path / "psx"
    _write(folder / "Loose.bin")
    _write(folder / "Other.iso")

    marks = await _listing(folder)

    assert marks == {"Loose.bin": (None, None), "Other.iso": (None, None)}


@pytest.mark.asyncio
async def test_a_playlists_discs_get_disc_of(tmp_path):
    folder = tmp_path / "psx"
    disc1 = _cue(folder, "Game (Disc 1).cue", ["Game (Disc 1).bin"])
    disc2 = _write(folder / "Game (Disc 2).chd")
    m3u = _write(folder / "Game.m3u", b"Game (Disc 1).cue\r\n./Game (Disc 2).chd\n")

    marks = await _listing(folder)

    assert marks["Game (Disc 1).cue"] == (None, str(m3u))
    assert marks["Game (Disc 2).chd"] == (None, str(m3u))
    # A disc keeps its own tracks under it.
    assert marks["Game (Disc 1).bin"] == (str(disc1), None)
    assert marks["Game.m3u"] == (None, None)
    assert disc2.exists()


@pytest.mark.asyncio
async def test_an_unreadable_sheet_marks_nothing(tmp_path):
    folder = tmp_path / "psx"
    _write(folder / "Game.bin")
    sheet = _write(folder / "Game.cue", b'FILE "Game.bin" BINARY\n')
    sheet.chmod(0)
    try:
        marks = await _listing(folder)
    finally:
        sheet.chmod(0o644)

    assert marks["Game.bin"] == (None, None)


@pytest.mark.asyncio
async def test_an_unreadable_playlist_marks_nothing(tmp_path):
    folder = tmp_path / "psx"
    _write(folder / "Game (Disc 1).chd")
    playlist = _write(folder / "Game.m3u", b"Game (Disc 1).chd\n")
    playlist.chmod(0)
    try:
        marks = await _listing(folder)
    finally:
        playlist.chmod(0o644)

    assert marks["Game (Disc 1).chd"] == (None, None)


@pytest.mark.asyncio
async def test_a_stalled_pass_marks_nothing_and_still_lists(tmp_path, monkeypatch):
    from routes import files as files_routes
    from services import image_sets

    folder = tmp_path / "psx"
    _cue(folder, "Game.cue", ["Game.bin"])
    _write(folder / "Game.m3u", b"Game.cue\n")
    release = threading.Event()
    real = image_sets.image_members

    def hangs(path, **kwargs):
        if path.endswith(".cue"):
            release.wait(5)
        return real(path, **kwargs)

    monkeypatch.setattr(image_sets, "image_members", hangs)
    monkeypatch.setattr(files_routes, "WALK_STALL_SECONDS", 0.1)
    try:
        marks = await _listing(folder)
    finally:
        release.set()

    assert set(marks) == {"Game.cue", "Game.bin", "Game.m3u"}
    assert all(m == (None, None) for m in marks.values()), marks
