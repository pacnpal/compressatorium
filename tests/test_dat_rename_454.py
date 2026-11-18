"""Rename to DAT name holds back only what an unresolvable sheet may name (#454).

A ``.cue`` whose tracks can't be told (an absolute ``FILE`` path, or a sheet
the app can't read) used to make the rename skip every Image in every Volume.
Such a sheet can only name files in its own folder and below (``resolve_track``
refuses an absolute name and any ``..``), reached through a link anywhere in
the Volumes. Those Images are still held back; every other Image renames.

Driven through the HTTP API on real files, as #425's tests are.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from . import test_dat_rename_425 as rename_425

_db_fixture = rename_425._db_fixture
_sent = rename_425._sent
_client = rename_425._client

_by_path = rename_425._by_path
_dat = rename_425._dat
_disc = rename_425._disc
_import = rename_425._import
_match = rename_425._match
_preview = rename_425._preview

GAME = "Ren Conflict (USA)"
HELD = "can't tell whether a sheet lists it as a track"


def _absolute_sheet(folder: Path) -> Path:
    """A ``.cue`` naming its track by an absolute path, and that track."""
    track = folder / "Abs Game.bin"
    _disc(track, b"abs track")
    sheet = folder / "Abs Game.cue"
    sheet.write_text(f'FILE "{track}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    return sheet


def _unreadable_sheet(folder: Path) -> Path:
    """A ``.cue`` the app can't read, and its track."""
    _disc(folder / "Unread Game.bin", b"unread track")
    sheet = folder / "Unread Game.cue"
    sheet.write_text('FILE "Unread Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    sheet.chmod(0)
    return sheet


async def _recognized(client: httpx.AsyncClient, *games: tuple[Path, str]) -> None:
    """Each ``(path, game name)`` written and Recognized by one DAT."""
    sha1s = {name: _disc(path, name.encode()) for path, name in games}
    await _import(client, _dat("Nintendo - Game Boy Advance", sha1s, homepage="no-intro.org"))
    await _match(client, *(path for path, _ in games))


@pytest.mark.asyncio
async def test_an_unrelated_image_renames_beside_a_sheet_naming_an_absolute_track(
    db: Path, client: httpx.AsyncClient,
) -> None:
    _absolute_sheet(db / "inbox" / "psx" / "abs")
    game = db / "ren" / "gba" / "Ren Conflict.gba"
    await _recognized(client, (game, GAME))

    item = _by_path(await _preview(client, game))[str(game)]

    assert item["status"] == "rename", item
    assert item["new_path"] == str(game.parent / f"{GAME}.gba")


@pytest.mark.asyncio
async def test_an_image_in_or_below_the_unresolvable_sheets_folder_is_still_held(
    db: Path, client: httpx.AsyncClient,
) -> None:
    folder = db / "inbox" / "psx" / "abs"
    _absolute_sheet(folder)
    beside, below = folder / "Beside.gba", folder / "deeper" / "Below.gba"
    await _recognized(client, (beside, "Beside (USA)"), (below, "Below (USA)"))

    items = _by_path(await _preview(client, beside, below))

    for path in (beside, below):
        assert items[str(path)]["status"] == "skip", items[str(path)]
        assert HELD in items[str(path)]["reason"]
        assert "Abs Game.cue" in items[str(path)]["reason"]


@pytest.mark.asyncio
async def test_an_image_the_unresolvable_sheet_reaches_through_a_link_is_still_held(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """The sheet's folder holds a link to another folder and a hard link to
    another Image: a track name through either reaches them, so both stay."""
    folder = db / "inbox" / "psx" / "abs"
    _absolute_sheet(folder)
    linked = db / "elsewhere" / "Linked.gba"
    hard = db / "other" / "Hard.gba"
    free = db / "free" / "Free.gba"
    await _recognized(
        client, (linked, "Linked (USA)"), (hard, "Hard (USA)"), (free, "Free (USA)"),
    )
    (folder / "link").symlink_to(linked.parent, target_is_directory=True)
    os.link(hard, folder / "hard.gba")

    items = _by_path(await _preview(client, linked, hard, free))

    for path in (linked, hard):
        assert items[str(path)]["status"] == "skip", items[str(path)]
        assert HELD in items[str(path)]["reason"]
    assert items[str(free)]["status"] == "rename", items[str(free)]


@pytest.mark.asyncio
@pytest.mark.skipif(rename_425.folder_library_303._IS_ROOT, reason="root reads any file")
async def test_an_unreadable_sheet_holds_only_its_own_folder_too(
    db: Path, client: httpx.AsyncClient,
) -> None:
    folder = db / "inbox" / "psx" / "unread"
    sheet = _unreadable_sheet(folder)
    beside = folder / "Beside.gba"
    game = db / "ren" / "gba" / "Ren Conflict.gba"
    try:
        await _recognized(client, (beside, "Beside (USA)"), (game, GAME))

        items = _by_path(await _preview(client, beside, game))
    finally:
        sheet.chmod(0o644)

    assert items[str(game)]["status"] == "rename", items[str(game)]
    assert items[str(beside)]["status"] == "skip", items[str(beside)]
    assert HELD in items[str(beside)]["reason"]
    assert "Unread Game.cue" in items[str(beside)]["reason"]


@pytest.mark.asyncio
async def test_a_sheets_track_an_unresolvable_sheet_may_also_name_is_held(
    db: Path, client: httpx.AsyncClient,
) -> None:
    """A good sheet elsewhere whose track the unresolvable sheet's folder
    holds a hard link to isn't known to be named by that good sheet alone."""
    from . import test_dat_rename_426 as rename_426

    files = await rename_426._cue_image(client, db / "good" / "disc")
    folder = db / "inbox" / "psx" / "abs"
    _absolute_sheet(folder)
    os.link(files["one"], folder / "track.bin")

    item = _by_path(await _preview(client, files["sheet"]))[str(files["sheet"])]

    assert item["status"] == "skip", item
    assert "may be listed by a sheet whose tracks can't be told" in item["reason"], item
    assert "Abs Game.cue" in item["reason"]
