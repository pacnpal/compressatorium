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
_result = rename_425._result
_xml = rename_425._xml
CONFIRM = rename_425.CONFIRM


def _confirmed(preview: dict) -> list[dict]:
    """Each preview item as the dialog confirms it: its new path and each
    file's (CodeRabbit 4604333228)."""
    return [
        {
            "path": i["path"], "new_path": i["new_path"],
            "files": [{"path": f["path"], "new_path": f["new_path"]} for f in i["files"]],
        }
        for i in preview["items"]
    ]


async def _run_items(client: httpx.AsyncClient, items: list[dict]) -> dict:
    """Confirm *items* and wait for the job; the finished job."""
    from services import dat_rename

    started = await client.post("/api/files/dat-rename", json={"items": items}, headers=CONFIRM)
    assert started.status_code == 200, started.text
    await dat_rename.drain()
    job = await client.get(f"/api/jobs/{started.json()['job_id']}")
    assert job.status_code == 200, job.text
    return job.json()


async def _run(client: httpx.AsyncClient, preview: dict) -> dict:
    return await _run_items(client, _confirmed(preview))

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
    # Each file moves through the shared anchored move (#431's
    # `path_utils.move_anchored`); the last track's move fails.
    real_move = image_rename.move_anchored
    seen: list[list[str]] = []

    def fails_on_the_last_track(name, src, dst, expected, **kwargs):
        if name == image["two"].name:
            # Everything before it has been renamed, the sheet rewritten.
            seen.append(sorted(p.name for p in (psx / "game").iterdir()))
            raise OSError(5, "Input/output error")
        return real_move(name, src, dst, expected, **kwargs)

    monkeypatch.setattr(image_rename, "move_anchored", fails_on_the_last_track)
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
async def test_a_run_whose_files_differ_from_the_plan_renames_nothing(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """CodeRabbit 4604333228: one track's confirmed new name isn't the
    plan's, so the Image is skipped and no file changes."""
    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx)
    await _register(client, db / "roms")
    items = _confirmed(await _preview(client, image["sheet"]))
    items[0]["files"][-1]["new_path"] = str(psx / "Something Else.bin")

    job = await _run_items(client, items)

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "skipped", result
    assert "files' new names changed" in result["reason"]
    assert sorted(p.name for p in psx.iterdir()) == sorted([
        "game.cue", "game (Track 1).bin", "game (Track 2).bin",
    ])


@pytest.mark.asyncio
async def test_a_run_that_confirms_only_the_sheets_path_renames_nothing(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """CodeRabbit 4604333160: a client that sends no ``files`` confirmed one
    path; an Image of several files needs every file confirmed."""
    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx)
    await _register(client, db / "roms")
    preview = await _preview(client, image["sheet"])

    job = await _run_items(client, [
        {"path": i["path"], "new_path": i["new_path"]} for i in preview["items"]
    ])

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "skipped", result
    assert sorted(p.name for p in psx.iterdir()) == sorted([
        "game.cue", "game (Track 1).bin", "game (Track 2).bin",
    ])


@pytest.mark.asyncio
async def test_tracks_whose_bytes_are_the_same_are_each_named_from_the_dat(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """CodeRabbit 4604333173: two silent tracks share their bytes, so the
    hash index keeps one row for both; the DAT's file list keeps both."""
    psx = db / "roms" / "psx"
    sheet = psx / "game.cue"
    text = CUE + 'FILE "game (Track 3).bin" BINARY\r\n  TRACK 03 AUDIO\r\n    INDEX 01 00:00:00\r\n'
    roms = {
        f"{GAME}.cue": _sheet(sheet, text),
        f"{GAME} (Track 1).bin": _track(psx / "game (Track 1).bin", b"data", 4704),
        f"{GAME} (Track 2).bin": _track(psx / "game (Track 2).bin", b"silence", 2352),
        f"{GAME} (Track 3).bin": _track(psx / "game (Track 3).bin", b"silence", 2352),
    }
    await _import(client, _dat("Sony - PlayStation", GAME, roms))
    await _match(client, sheet)
    await _register(client, db / "roms")

    job = await _run(client, await _preview(client, sheet))

    assert _result(job)[str(sheet)]["status"] == "renamed"
    assert sorted(p.name for p in psx.iterdir()) == sorted(roms)


@pytest.mark.asyncio
async def test_a_dats_file_list_goes_with_it(db: Path, client: httpx.AsyncClient) -> None:
    from services.dat_store import dat_store

    roms = {f"{GAME}.cue": ("a" * 40, 10), f"{GAME} (Track 1).bin": ("b" * 40, 20)}
    dat_id = await _import(client, _dat("Sony - PlayStation", GAME, roms))
    assert [f["rom_name"] for f in dat_store.game_files(dat_id, GAME)] == sorted(roms)

    assert (await client.delete(f"/api/dat/{dat_id}")).status_code == 200

    assert dat_store.game_files(dat_id, GAME) == []


@pytest.mark.asyncio
async def test_a_gamelist_entry_for_the_renamed_folder_follows_it(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Codex 4604233211: an ES-DE ``<folder>`` entry names the folder itself."""
    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    gamelist = psx / "gamelist.xml"
    gamelist.write_text(
        '<?xml version="1.0"?>\n<gameList>\n'
        "\t<folder>\n\t\t<path>./game</path>\n\t\t<name>Ape</name>\n\t</folder>\n"
        "\t<game>\n\t\t<path>./game/game.cue</path>\n\t\t<name>Ape</name>\n\t</game>\n"
        "</gameList>\n",
    )
    await _register(client, db / "roms", es_frontend=True)

    preview = await _preview(client, image["sheet"])
    (item,) = preview["items"]
    assert item["status"] == "rename", item
    (change,) = item["referrer_changes"]
    assert change["entries"] == 2, change

    job = await _run(client, preview)

    assert _result(job)[str(image["sheet"])]["status"] == "renamed"
    text = gamelist.read_text()
    assert f"<path>./{GAME}</path>" in text
    assert f"<path>./{GAME}/{GAME}.cue</path>" in text


@pytest.mark.asyncio
async def test_a_windows_1252_sheet_has_its_reference_rewritten_as_it_was_resolved(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """Codex 4604233197: the sheet names ``café`` in Windows-1252 and the
    file is spelled in UTF-8; the reference found is the one rewritten."""
    psx = db / "roms" / "psx"
    game = "Cafe Racer (Europe)"
    raw = (
        b"REM caf\xe9\r\n"
        b'FILE "caf\xe9 (Track 1).bin" BINARY\r\n'
        b"  TRACK 01 MODE1/2352\r\n    INDEX 01 00:00:00\r\n"
    )
    sheet = psx / "game.cue"
    psx.mkdir(parents=True)
    sheet.write_bytes(raw)
    roms = {
        f"{game}.cue": (hashlib.sha1(raw).hexdigest(), len(raw)),
        f"{game} (Track 1).bin": _track(psx / "café (Track 1).bin", b"data", 4704),
    }
    await _import(client, _dat("Sony - PlayStation", game, roms))
    await _match(client, sheet)
    await _register(client, db / "roms")

    job = await _run(client, await _preview(client, sheet))

    assert _result(job)[str(sheet)]["status"] == "renamed"
    assert (psx / f"{game}.cue").read_bytes() == raw.replace(
        b'"caf\xe9 (Track 1).bin"', f'"{game} (Track 1).bin"'.encode(),
    )
    assert (psx / f"{game} (Track 1).bin").exists()


async def _assert_untouched(client, db: Path, monkeypatch, seam: str, broken) -> None:
    """Break the sheet rewrite's *seam* with *broken*: the Image is not
    renamed, and the sheet, its tracks, the folder and the playlist end
    exactly as they were (Codex 4604210916, 4604210926)."""
    from services import image_rename

    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    tracks = {image["one"]: image["one"].read_bytes(), image["two"]: image["two"].read_bytes()}
    playlist = psx / "games.m3u"
    playlist.write_text("game/game.cue\n")
    await _register(client, db / "roms")
    preview = await _preview(client, image["sheet"])
    monkeypatch.setattr(image_rename, seam, broken)

    job = await _run(client, preview)

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "not_renamed", result
    assert sorted(p.name for p in psx.iterdir()) == ["game", "games.m3u", "games.m3u.bak"]
    assert sorted(p.name for p in (psx / "game").iterdir()) == sorted([
        "game.cue", "game (Track 1).bin", "game (Track 2).bin",
    ])
    assert image["sheet"].read_bytes() == CUE.encode()
    assert all(track.read_bytes() == data for track, data in tracks.items())
    assert playlist.read_text() == "game/game.cue\n"


@pytest.mark.asyncio
async def test_a_write_the_disk_stops_accepting_leaves_everything_as_it_was(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    def accepts_nothing(fd, data):
        return 0

    await _assert_untouched(client, db, monkeypatch, "_write", accepts_nothing)


@pytest.mark.asyncio
async def test_an_fsync_that_fails_leaves_everything_as_it_was(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    def fails(fd):
        raise OSError(5, "Input/output error")

    await _assert_untouched(client, db, monkeypatch, "_fsync", fails)


@pytest.mark.asyncio
async def test_short_writes_are_written_on_until_the_whole_sheet_is(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    import os

    from services import image_rename

    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx)
    await _register(client, db / "roms")
    preview = await _preview(client, image["sheet"])
    monkeypatch.setattr(image_rename, "_write", lambda fd, data: os.write(fd, bytes(data[:7])))

    job = await _run(client, preview)

    assert _result(job)[str(image["sheet"])]["status"] == "renamed"
    assert (psx / f"{GAME}.cue").read_bytes() == RENAMED_CUE.encode()


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



@pytest.mark.asyncio
async def test_a_committed_placement_is_rolled_back_with_the_rest(
    db: Path, client: httpx.AsyncClient, monkeypatch,
) -> None:
    """CodeRabbit 4606148011: a move that raises PlacementCommitted (the file
    is at its new name) counts as moved, so the rollback puts it back too."""
    from services import image_rename
    from utils.path_utils import PlacementCommitted

    psx = db / "roms" / "psx"
    image = await _cue_image(client, psx / "game")
    await _register(client, db / "roms")
    preview = await _preview(client, image["sheet"])
    real_move = image_rename.move_anchored

    def committed_on_the_last_track(name, src, dst, expected, **kwargs):
        real_move(name, src, dst, expected, **kwargs)
        if name == image["two"].name:
            raise PlacementCommitted(5, "Input/output error after the move")

    monkeypatch.setattr(image_rename, "move_anchored", committed_on_the_last_track)
    job = await _run(client, preview)

    result = _result(job)[str(image["sheet"])]
    assert result["status"] == "not_renamed", result
    assert sorted(p.name for p in (psx / "game").iterdir()) == sorted([
        "game.cue", "game (Track 1).bin", "game (Track 2).bin",
    ])
