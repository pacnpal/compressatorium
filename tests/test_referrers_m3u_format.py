"""The ``.m3u`` Referrer keeps everything it doesn't need to change (#295).

Driven through the Referrer hook the job calls, on a temp folder.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from services import referrers
from services.referrers import m3u as m3u_module
from services.referrers.m3u import M3uReferrer

from config import settings


@pytest.fixture(autouse=True)
def _volume(tmp_path, monkeypatch):
    """tmp_path is the Volume, so ancestor playlists stop at it."""
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))


def _disc(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"disc")
    return path


async def _replace(folder: Path, old: str, new: str):
    return await referrers.update_before_source_removal(
        str(folder / old), {str(folder / old): str(folder / new)},
    )


@pytest.mark.asyncio
async def test_bom_and_non_utf8_bytes_survive(tmp_path):
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    # UTF-8 BOM, a Latin-1 byte in an unrelated entry, mixed endings, no final newline.
    playlist.write_bytes(
        b"\xef\xbb\xbfGame (Disc 1).cue\n# Pok\xe9mon\r\nPok\xe9mon.cue\rlast.cue"
    )

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert outcome.ok
    assert playlist.read_bytes() == (
        b"\xef\xbb\xbfGame (Disc 1).chd\n# Pok\xe9mon\r\nPok\xe9mon.cue\rlast.cue"
    )


@pytest.mark.asyncio
async def test_absolute_and_backslash_entries_keep_their_style(tmp_path):
    _disc(tmp_path, "A.cue")
    (tmp_path / "sub").mkdir()
    _disc(tmp_path / "sub", "B.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text(f"{tmp_path / 'A.cue'}\nsub\\B.cue\n")

    await referrers.update_before_source_removal(
        str(tmp_path / "A.cue"), {str(tmp_path / "A.cue"): str(tmp_path / "A.chd")},
    )
    assert playlist.read_text() == f"{tmp_path / 'A.chd'}\nsub\\B.cue\n"

    # A disc in a sub-folder: the playlist beside it and the one above it that
    # names it with backslashes are both rewritten.
    sub_playlist = tmp_path / "sub" / "B.m3u"
    sub_playlist.write_text("B.cue\n")
    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "sub" / "B.cue"),
        {str(tmp_path / "sub" / "B.cue"): str(tmp_path / "sub" / "B.chd")},
    )
    assert outcome.ok
    assert sub_playlist.read_text() == "B.chd\n"
    assert playlist.read_text() == f"{tmp_path / 'A.chd'}\nsub\\B.chd\n"


@pytest.mark.asyncio
async def test_playlist_in_an_ancestor_folder_is_rewritten(tmp_path):
    """Batocera / ES-DE layout: playlist in the system folder, discs hidden below."""
    hidden = tmp_path / "psx" / ".discs"
    hidden.mkdir(parents=True)
    _disc(hidden, "Game (Disc 1).cue")
    system = tmp_path / "psx" / "Game.m3u"
    system.write_text(".discs/Game (Disc 1).cue\n.discs/Game (Disc 2).cue\n")
    root = tmp_path / "All.m3u"
    root.write_text("psx/.discs/Game (Disc 1).cue\n")

    outcome = await _replace(hidden, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert outcome.ok
    assert system.read_text() == ".discs/Game (Disc 1).chd\n.discs/Game (Disc 2).cue\n"
    # Up to and including the Volume root.
    assert root.read_text() == "psx/.discs/Game (Disc 1).chd\n"


@pytest.mark.asyncio
async def test_playlists_above_the_volume_root_are_not_read(tmp_path, monkeypatch):
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    _disc(volume, "A.cue")
    outside = tmp_path / "Outside.m3u"
    outside.write_text("vol/A.cue\n")

    outcome = await _replace(volume, "A.cue", "A.chd")

    assert outcome.ok
    assert outside.read_text() == "vol/A.cue\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [
    "Game.cue|Disc 1",                 # RetroArch path|label
    "/userdata/roms/psx/Game.cue",     # the emulator's own path space
    "sub\\dir/Game.cue",               # mixed separators
    '"Game.cue"',                      # quoted
    "OTHER/GAME.CUE",                  # different case, different folder
])
async def test_an_entry_that_names_the_source_but_does_not_resolve_keeps_it(
    tmp_path, entry,
):
    _disc(tmp_path, "Game.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text(f"#EXTM3U\n{entry}\n")

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert not outcome.ok
    assert outcome.failed == str(playlist)
    assert "line 2" in outcome.kept_message
    assert outcome.kept_message.startswith(
        "converted, source kept: couldn't update Game.m3u"
    )
    assert playlist.read_text() == f"#EXTM3U\n{entry}\n"
    assert not (tmp_path / "Game.m3u.bak").exists()


@pytest.mark.asyncio
async def test_a_broken_entry_for_another_file_does_not_block(tmp_path):
    _disc(tmp_path, "Game.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\nMissing Other.cue\n")

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok
    assert playlist.read_text() == "Game.chd\nMissing Other.cue\n"


@pytest.mark.asyncio
async def test_two_discs_updating_one_playlist_at_once_keep_both_edits(
    tmp_path, monkeypatch,
):
    _disc(tmp_path, "D1.cue")
    _disc(tmp_path, "D2.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("D1.cue\nD2.cue\n")
    real_write = m3u_module.atomic_write_bytes

    def slow_write(path, data):
        time.sleep(0.2)
        real_write(path, data)

    monkeypatch.setattr(m3u_module, "atomic_write_bytes", slow_write)

    first, second = await asyncio.gather(
        _replace(tmp_path, "D1.cue", "D1.chd"),
        _replace(tmp_path, "D2.cue", "D2.chd"),
    )

    assert first.ok and second.ok
    assert playlist.read_text() == "D1.chd\nD2.chd\n"


@pytest.mark.asyncio
async def test_symlinked_folder_gives_a_plain_entry(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    _disc(real, "Game.cue")
    playlist = real / "Game.m3u"
    playlist.write_text("Game.cue\n./Game.cue\n")

    # The job passes resolved Source paths and the output as planned.
    outcome = await referrers.update_before_source_removal(
        str(real / "Game.cue"),
        {str(real / "Game.cue"): str(tmp_path / "link" / "Game.chd")},
    )

    assert outcome.ok
    assert playlist.read_text() == "Game.chd\n./Game.chd\n"


@pytest.mark.asyncio
async def test_backup_is_written_once(tmp_path):
    _disc(tmp_path, "Game (Disc 1).cue")
    _disc(tmp_path, "Game (Disc 2).cue")
    playlist = tmp_path / "Game.m3u"
    original = b"Game (Disc 1).cue\nGame (Disc 2).cue\n"
    playlist.write_bytes(original)

    await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")
    await _replace(tmp_path, "Game (Disc 2).cue", "Game (Disc 2).chd")

    assert playlist.read_bytes() == b"Game (Disc 1).chd\nGame (Disc 2).chd\n"
    # The backup is the playlist as it was before the app first touched it.
    assert (tmp_path / "Game.m3u.bak").read_bytes() == original


def test_an_edit_made_after_planning_is_kept(tmp_path):
    """apply re-reads the playlist under its lock and rewrites what is there now."""
    _disc(tmp_path, "A.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    m3u = M3uReferrer()
    [change] = m3u.plan({str(tmp_path / "A.cue"): str(tmp_path / "A.chd")})

    playlist.write_text("A.cue\nB.cue\n")
    m3u.apply(change)

    assert playlist.read_text() == "A.chd\nB.cue\n"


@pytest.mark.asyncio
async def test_no_playlist_means_nothing_to_do(tmp_path):
    _disc(tmp_path, "A.cue")

    outcome = await _replace(tmp_path, "A.cue", "A.chd")

    assert outcome.ok and outcome.updated == []
    assert list(tmp_path.iterdir()) == [tmp_path / "A.cue"]


def test_a_second_registration_of_an_id_is_refused():
    with pytest.raises(ValueError):
        referrers.registry.register(M3uReferrer())
