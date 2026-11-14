"""The ``.m3u`` Referrer keeps everything it doesn't need to change (#295).

Driven through the Referrer hook the job calls, on a temp folder.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from services import referrers
from services.referrers.m3u import M3uReferrer


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

    # A playlist only follows files beside it, so a sub-folder disc needs a
    # playlist in that sub-folder; one naming it from above is left alone.
    sub_playlist = tmp_path / "sub" / "B.m3u"
    sub_playlist.write_text("B.cue\n")
    await referrers.update_before_source_removal(
        str(tmp_path / "sub" / "B.cue"),
        {str(tmp_path / "sub" / "B.cue"): str(tmp_path / "sub" / "B.chd")},
    )
    assert sub_playlist.read_text() == "B.chd\n"


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


def test_a_playlist_edited_after_planning_is_not_overwritten(tmp_path):
    _disc(tmp_path, "A.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    m3u = M3uReferrer()
    [change] = m3u.plan({str(tmp_path / "A.cue"): str(tmp_path / "A.chd")})

    playlist.write_text("A.cue\nB.cue\n")

    with pytest.raises(referrers.ReferrerError):
        m3u.apply(change)
    assert playlist.read_text() == "A.cue\nB.cue\n"


@pytest.mark.asyncio
async def test_no_playlist_means_nothing_to_do(tmp_path):
    _disc(tmp_path, "A.cue")

    outcome = await _replace(tmp_path, "A.cue", "A.chd")

    assert outcome.ok and outcome.updated == []
    assert list(tmp_path.iterdir()) == [tmp_path / "A.cue"]


def test_a_second_registration_of_an_id_is_refused():
    with pytest.raises(ValueError):
        referrers.registry.register(M3uReferrer())
