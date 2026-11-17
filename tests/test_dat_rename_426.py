"""Rename multi-file Images to their DAT name (#426).

A cue or gdi Image is renamed as a whole: the sheet takes the DAT's game
name, each track takes the DAT's own file name for it, and the sheet's track
references are rewritten to the new names with every other byte kept. A
folder that holds only that Image and carries its name is renamed too. Every
file is renamed, or none is: a failure part way puts back what was already
renamed, the sheet's bytes and every Referrer.

Driven through the HTTP API on real files, as #425's tests are (DATs imported
through ``/dat/import``, the sheet matched through ``/dat/match-batch``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from . import test_dat_rename_425 as rename_425

_db_fixture = rename_425._db_fixture
_sent = rename_425._sent
_client = rename_425._client

_import = rename_425._import
_match = rename_425._match
_preview = rename_425._preview
_register = rename_425._register
_run = rename_425._run
_result = rename_425._result
_xml = rename_425._xml

GAME = "Ape Escape (USA)"
CUE = (
    'REM made by hand\r\n'
    'FILE "game (Track 1).bin" BINARY\r\n'
    "  TRACK 01 MODE2/2352\r\n"
    "    INDEX 01 00:00:00\r\n"
    'FILE "game (Track 2).bin" BINARY\r\n'
    "  TRACK 02 AUDIO\r\n"
    "    INDEX 00 00:00:00\r\n"
    "    INDEX 01 00:02:00\r\n"
)
RENAMED_CUE = CUE.replace("game (Track", f"{GAME} (Track")


def _track(path: Path, seed: bytes, size: int) -> tuple[str, int]:
    """A track of *size* bytes made from *seed*; its SHA1 and size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (hashlib.sha256(seed).digest() * (size // 32 + 1))[:size]
    path.write_bytes(data)
    return hashlib.sha1(data).hexdigest(), size


def _sheet(path: Path, text: str) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode()
    path.write_bytes(data)
    return hashlib.sha1(data).hexdigest(), len(data)


def _dat(name: str, game: str, roms: dict[str, tuple[str, int]]) -> str:
    """A DAT with one *game* whose files are *roms* (``{file name: (sha1, size)}``)."""
    body = "".join(
        f'<rom name="{_xml(rom)}" size="{size}" sha1="{sha1}"/>'
        for rom, (sha1, size) in roms.items()
    )
    return (
        '<?xml version="1.0"?><datafile><header>'
        f"<name>{name}</name><description>{name}</description>"
        "<homepage>redump.org</homepage></header>"
        f'<game name="{_xml(game)}">{body}</game></datafile>'
    )


async def _cue_image(client: httpx.AsyncClient, folder: Path) -> dict[str, Path]:
    """A two-track cue Image in *folder*, Recognized by a Redump-style DAT
    whose tracks are named after the game. Both tracks are the same size, so
    they pair with the DAT's files in track order."""
    sheet = folder / "game.cue"
    one, two = folder / "game (Track 1).bin", folder / "game (Track 2).bin"
    roms = {
        f"{GAME}.cue": _sheet(sheet, CUE),
        # Listed out of order on purpose: the DAT's own order isn't relied on.
        f"{GAME} (Track 2).bin": _track(two, b"audio", 4704),
        f"{GAME} (Track 1).bin": _track(one, b"data", 4704),
    }
    await _import(client, _dat("Sony - PlayStation", GAME, roms))
    await _match(client, sheet)
    return {"sheet": sheet, "one": one, "two": two}


@pytest.mark.asyncio
async def test_a_cue_with_several_tracks_is_renamed_whole_and_its_references_rewritten(
    db: Path, client: httpx.AsyncClient,
) -> None:
    from services.dat_store import dat_store

    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx)
    data_one, data_two = image["one"].read_bytes(), image["two"].read_bytes()
    (psx / "games.m3u").write_text("game.cue\n")
    await _register(client, db / "roms")

    preview = await _preview(client, image["sheet"])

    (item,) = preview["items"]
    assert item["status"] == "rename", item
    new_sheet = psx / f"{GAME}.cue"
    new_one, new_two = psx / f"{GAME} (Track 1).bin", psx / f"{GAME} (Track 2).bin"
    assert item["new_path"] == str(new_sheet)
    assert {f["path"]: f["new_path"] for f in item["files"]} == {
        str(image["sheet"]): str(new_sheet),
        str(image["one"]): str(new_one),
        str(image["two"]): str(new_two),
    }
    assert item["sheet_rewrite"] == [
        {"old": "game (Track 1).bin", "new": f"{GAME} (Track 1).bin"},
        {"old": "game (Track 2).bin", "new": f"{GAME} (Track 2).bin"},
    ]
    assert item["folder"] is None
    (change,) = item["referrer_changes"]
    assert change["edits"] == [{"old": "game.cue", "new": f"{GAME}.cue"}]
    # The preview changes nothing.
    assert image["sheet"].read_text(newline="") == CUE

    job = await _run(client, preview)

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "renamed", result
    assert sorted(p.name for p in psx.iterdir()) == sorted([
        new_sheet.name, new_one.name, new_two.name, "games.m3u", "games.m3u.bak",
    ])
    # Only the track names changed in the sheet; every other byte is kept.
    assert new_sheet.read_bytes() == RENAMED_CUE.encode()
    assert new_one.read_bytes() == data_one and new_two.read_bytes() == data_two
    assert (psx / "games.m3u").read_text() == f"{GAME}.cue\n"
    assert dat_store.get_match(str(new_sheet))["game_name"] == GAME
    assert dat_store.get_match(str(image["sheet"])) is None


@pytest.mark.asyncio
async def test_a_sheet_already_named_still_gets_its_tracks_and_references_renamed(
    db: Path, client: httpx.AsyncClient,
) -> None:
    psx = db / "roms" / "psx"
    sheet = psx / f"{GAME}.cue"
    roms = {
        f"{GAME}.cue": _sheet(sheet, CUE),
        f"{GAME} (Track 1).bin": _track(psx / "game (Track 1).bin", b"data", 4704),
        f"{GAME} (Track 2).bin": _track(psx / "game (Track 2).bin", b"audio", 2352),
    }
    await _import(client, _dat("Sony - PlayStation", GAME, roms))
    await _match(client, sheet)
    await _register(client, db / "roms")

    preview = await _preview(client, sheet)
    (item,) = preview["items"]
    assert item["status"] == "rename", item
    assert item["new_path"] == str(sheet)

    job = await _run(client, preview)

    result = _result(job)[str(sheet)]
    assert result["status"] == "renamed", result
    assert sorted(p.name for p in psx.iterdir()) == sorted(roms)
    assert sheet.read_bytes() == RENAMED_CUE.encode()


GDI = (
    "3\n"
    "1 0 4 2352 track01.bin 0\n"
    "2 756 0 2352 track02.raw 0\n"
    '3 45000 4 2352 "track03.bin" 0\n'
)


@pytest.mark.asyncio
async def test_a_gdi_is_renamed_whole_with_its_tracks_named_as_the_dat_names_them(
    db: Path, client: httpx.AsyncClient,
) -> None:
    dc = db / "roms" / "dc"
    sheet = dc / "disc.gdi"
    game = "Crazy Taxi (USA)"
    roms = {
        f"{game}.gdi": _sheet(sheet, GDI),
        f"{game} (Track 1).bin": _track(dc / "track01.bin", b"one", 2352 * 3),
        f"{game} (Track 2).raw": _track(dc / "track02.raw", b"two", 2352 * 2),
        f"{game} (Track 3).bin": _track(dc / "track03.bin", b"three", 2352 * 4),
    }
    await _import(client, _dat("Sega - Dreamcast", game, roms))
    await _match(client, sheet)
    await _register(client, db / "roms")

    job = await _run(client, await _preview(client, sheet))

    result = _result(job)[str(sheet)]
    assert result["status"] == "renamed", result
    assert sorted(p.name for p in dc.iterdir()) == sorted(roms)
    assert (dc / f"{game}.gdi").read_text(newline="") == (
        "3\n"
        f"1 0 4 2352 \"{game} (Track 1).bin\" 0\n"
        f"2 756 0 2352 \"{game} (Track 2).raw\" 0\n"
        f'3 45000 4 2352 "{game} (Track 3).bin" 0\n'
    )


@pytest.mark.asyncio
async def test_a_folder_holding_only_the_image_and_named_after_it_is_renamed_too(
    db: Path, client: httpx.AsyncClient,
) -> None:
    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    (psx / "games.m3u").write_text("game/game.cue\n")
    await _register(client, db / "roms")

    preview = await _preview(client, image["sheet"])

    (item,) = preview["items"]
    assert item["status"] == "rename", item
    folder = psx / GAME
    assert item["folder"] == {"path": str(psx / "game"), "new_path": str(folder)}
    assert item["new_path"] == str(folder / f"{GAME}.cue")

    job = await _run(client, preview)

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "renamed", result
    assert not (psx / "game").exists()
    assert sorted(p.name for p in folder.iterdir()) == sorted([
        f"{GAME}.cue", f"{GAME} (Track 1).bin", f"{GAME} (Track 2).bin",
    ])
    assert (folder / f"{GAME}.cue").read_bytes() == RENAMED_CUE.encode()
    assert (psx / "games.m3u").read_text() == f"{GAME}/{GAME}.cue\n"


@pytest.mark.asyncio
async def test_a_folder_holding_anything_else_is_left_alone(
    db: Path, client: httpx.AsyncClient,
) -> None:
    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    (psx / "game" / "notes.txt").write_text("mine\n")
    await _register(client, db / "roms")

    preview = await _preview(client, image["sheet"])

    (item,) = preview["items"]
    assert item["status"] == "rename", item
    assert item["folder"] is None
    assert item["new_path"] == str(psx / "game" / f"{GAME}.cue")


@pytest.mark.asyncio
async def test_a_failure_on_the_last_track_puts_every_file_the_sheet_and_the_playlist_back(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    from services import image_rename
    from services.dat_store import dat_store

    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    playlist = psx / "games.m3u"
    playlist.write_text("game/game.cue\n")
    await _register(client, db / "roms")
    preview = await _preview(client, image["sheet"])
    real_place = image_rename.place_without_clobbering
    seen: list[list[str]] = []

    def fails_on_the_last_track(src, dst, **kwargs):
        if src == image["two"].name:
            # Everything before it has been renamed, the sheet rewritten.
            seen.append(sorted(p.name for p in (psx / "game").iterdir()))
            raise OSError(5, "Input/output error")
        return real_place(src, dst, **kwargs)

    monkeypatch.setattr(image_rename, "place_without_clobbering", fails_on_the_last_track)
    job = await _run(client, preview)

    assert seen == [sorted([f"{GAME}.cue", f"{GAME} (Track 1).bin", "game (Track 2).bin"])]
    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "not_renamed", result
    assert "Input/output error" in result["reason"]
    assert sorted(p.name for p in (psx / "game").iterdir()) == sorted([
        "game.cue", "game (Track 1).bin", "game (Track 2).bin",
    ])
    assert not (psx / GAME).exists()
    assert image["sheet"].read_text(newline="") == CUE
    assert playlist.read_text() == "game/game.cue\n"
    assert dat_store.get_match(str(image["sheet"]))["game_name"] == GAME


@pytest.mark.asyncio
async def test_tracks_that_dont_pair_with_the_dat_files_leave_the_image_alone(
    db: Path, client: httpx.AsyncClient,
) -> None:
    psx = db / "roms" / "psx"
    sheet = psx / "game.cue"
    roms = {
        f"{GAME}.cue": _sheet(sheet, CUE),
        f"{GAME} (Track 1).bin": _track(psx / "game (Track 1).bin", b"data", 4704),
        # The DAT's second track is another size than the file's.
        f"{GAME} (Track 2).bin": (hashlib.sha1(b"x").hexdigest(), 999),
    }
    _track(psx / "game (Track 2).bin", b"audio", 4704)
    await _import(client, _dat("Sony - PlayStation", GAME, roms))
    await _match(client, sheet)
    await _register(client, db / "roms")

    (item,) = (await _preview(client, sheet))["items"]

    assert item["status"] == "skip", item
    assert "game (Track 2).bin has no file of its size in the DAT" in item["reason"]


_romm = rename_425._romm


@pytest.mark.asyncio
async def test_a_cue_image_in_a_romm_library_is_left_alone_with_the_reason(
    db: Path, client: httpx.AsyncClient, romm,
) -> None:
    """RomM renames one file within its folder: renaming a cue's files under
    it would leave the ROM, and its saves, as a ghost."""
    platform = db / "romm" / "ps2"
    image = await _cue_image(client, platform)

    (item,) = (await _preview(client, image["sheet"]))["items"]

    assert item["status"] == "skip", item
    assert "RomM renames one file within its folder" in item["reason"]
    assert romm.renames == []
    assert image["sheet"].read_text(newline="") == CUE


# ----------------------------------------------------------------------
# the sheet rewrite, on bytes
# ----------------------------------------------------------------------


def test_the_sheet_rewrite_changes_only_the_track_names() -> None:
    from utils.sheet_text import rewrite_track_names

    cue = (
        b"\xef\xbb\xbfREM COMMENT \"game.bin\"\r\n"
        b"FILE \"game.bin\" BINARY\r\n"
        b"  TRACK 01 MODE1/2352\r\n"
        b"FILE other.bin   BINARY\n"
        b"FILE 'keep.bin' WAVE\n"
    )
    out = rewrite_track_names(cue, ".cue", {"game.bin": "Game (USA).bin", "other.bin": "B C.bin"})
    assert out == (
        b"\xef\xbb\xbfREM COMMENT \"game.bin\"\r\n"
        b"FILE \"Game (USA).bin\" BINARY\r\n"
        b"  TRACK 01 MODE1/2352\r\n"
        b"FILE \"B C.bin\"   BINARY\n"
        b"FILE 'keep.bin' WAVE\n"
    )
    gdi = b"2\r\n1 0 4 2352 sub/t1.bin 0\r\n2 600 0 2352 't2.raw' 0\r\n"
    assert rewrite_track_names(gdi, ".gdi", {"sub/t1.bin": "sub/T 1.bin", "t2.raw": "T2.raw"}) == (
        b"2\r\n1 0 4 2352 \"sub/T 1.bin\" 0\r\n2 600 0 2352 'T2.raw' 0\r\n"
    )
    # A name that can't be written back as one token is refused, never guessed.
    assert rewrite_track_names(cue, ".cue", {"game.bin": 'a"b.bin'}) is None
