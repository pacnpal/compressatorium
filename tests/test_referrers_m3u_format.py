"""The ``.m3u`` Referrer keeps everything it doesn't need to change (#295).

Driven through the Referrer hook the job calls, on a temp folder.
"""
from __future__ import annotations

import asyncio
import os
import stat
import time
import unicodedata
from pathlib import Path

import pytest
from services import referrers
from services.referrers import base as base_module
from services.referrers import m3u as m3u_module
from services.referrers.base import atomic_write_bytes
from services.referrers.m3u import M3uReferrer
from utils.path_utils import FileIdentity, file_identity, same_identity

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
    """The hook as the job calls it: the output already exists."""
    output = folder / new
    if not output.exists():
        output.write_bytes(b"output")
    return await referrers.update_before_source_removal(
        str(folder / old), {str(folder / old): str(folder / new)},
    )


@pytest.mark.asyncio
async def test_bom_endings_and_utf8_survive(tmp_path):
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    # UTF-8 BOM, non-ASCII in an unrelated entry, mixed endings, no final newline.
    data = "﻿Game (Disc 1).cue\n# Pokémon\r\nPokémon.cue\rlast.cue".encode()
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert outcome.ok
    assert playlist.read_bytes() == data.replace(b"(Disc 1).cue", b"(Disc 1).chd")


@pytest.mark.asyncio
async def test_a_non_utf8_playlist_naming_the_source_is_not_rewritten(tmp_path):
    """A Latin-1 byte anywhere means the file isn't UTF-8; it is never
    rewritten, so an entry naming the Source keeps the Source."""
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    data = b"\xef\xbb\xbfGame (Disc 1).cue\n# Pok\xe9mon\r\nPok\xe9mon.cue\rlast.cue"
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert not outcome.ok
    assert "isn't UTF-8" in outcome.reason
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
async def test_absolute_and_backslash_entries_keep_their_style(tmp_path):
    _disc(tmp_path, "A.cue")
    _disc(tmp_path, "A.chd")
    (tmp_path / "sub").mkdir()
    _disc(tmp_path / "sub", "B.cue")
    _disc(tmp_path / "sub", "B.chd")
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
@pytest.mark.parametrize(("entry", "rewritten"), [
    ("Game.cue|Disc 1", "Game.chd|Disc 1"),            # RetroArch path|label
    ("Game.cue | Disc 1", "Game.chd | Disc 1"),
    ('"Game.cue"', '"Game.chd"'),                      # quoted
    ("'Game.cue'|Disc 1", "'Game.chd'|Disc 1"),
])
async def test_a_labelled_or_quoted_entry_is_rewritten_keeping_its_form(
    tmp_path, entry, rewritten,
):
    """One reader: the label and quotes are read the way a reader reads them,
    so such an entry resolves to the Source and keeps its label and quotes."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text(f"#EXTM3U\n{entry}\n")

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok, outcome.reason
    assert playlist.read_text() == f"#EXTM3U\n{rewritten}\n"
    [read] = _reader_entries(playlist.read_bytes())
    assert os.path.samefile(tmp_path / read, out)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [
    "/userdata/roms/psx/Game.cue",     # the emulator's own path space
    "sub\\dir/Game.cue",               # mixed separators
    "OTHER/GAME.CUE",                  # different case, different folder
    "/userdata/roms/psx/Game.cue|Disc 1",
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

    def slow_write(path, data, **kw):
        time.sleep(0.2)
        real_write(path, data, **kw)

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
    _disc(real, "Game.chd")
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
    _disc(tmp_path, "A.chd")
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
    assert sorted(p.name for p in tmp_path.iterdir()) == ["A.chd", "A.cue"]


def test_a_second_registration_of_an_id_is_refused():
    with pytest.raises(ValueError):
        referrers.registry.register(M3uReferrer())


@pytest.mark.asyncio
async def test_symlinked_entry_folder_with_output_elsewhere_points_at_the_output(tmp_path):
    """An entry through a symlinked folder, output in another folder: the new
    entry must still reach the output (a lexical '..' collapse would not)."""
    (tmp_path / "x").mkdir()
    _disc(tmp_path / "x", "G.cue")
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "link").symlink_to(tmp_path / "x")
    playlist = tmp_path / "G.m3u"
    playlist.write_text("a/b/link/G.cue\n")
    (tmp_path / "out").mkdir()
    out = _disc(tmp_path / "out", "G.chd")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "x" / "G.cue"), {str(tmp_path / "x" / "G.cue"): str(out)},
    )

    assert outcome.ok, outcome.reason
    entry = playlist.read_text().strip()
    assert (tmp_path / entry).resolve() == out.resolve()


def test_an_entry_that_cannot_reach_the_output_keeps_the_source(tmp_path, monkeypatch):
    """Whatever the formatter produces is checked against the output."""
    _disc(tmp_path, "A.cue")
    (tmp_path / "out").mkdir()
    _disc(tmp_path / "out", "A.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    monkeypatch.setattr(m3u_module, "_new_entry", lambda *_a: ["nowhere/A.chd"])

    with pytest.raises(referrers.ReferrerError, match="line 1"):
        M3uReferrer().plan({str(tmp_path / "A.cue"): str(tmp_path / "out" / "A.chd")})
    assert playlist.read_text() == "A.cue\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"])
async def test_a_utf16_playlist_naming_the_source_keeps_it(tmp_path, encoding):
    _disc(tmp_path, "Game.cue")
    playlist = tmp_path / "Game.m3u"
    data = "Game.cue\n".encode(encoding)
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert not outcome.ok
    assert outcome.failed == str(playlist)
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
async def test_a_bomless_utf16_playlist_with_a_non_ascii_name_keeps_the_source(
    tmp_path, encoding,
):
    _disc(tmp_path, "Pokémon.cue")
    playlist = tmp_path / "P.m3u"
    data = "Pokémon.cue\n".encode(encoding)
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Pokémon.cue", "Pokémon.chd")

    assert not outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
async def test_a_playlist_beside_a_track_in_a_subfolder_is_updated(tmp_path):
    """The cue's track lives in tracks/, and only tracks/ has a playlist."""
    _disc(tmp_path, "Game.cue")
    (tmp_path / "tracks").mkdir()
    _disc(tmp_path / "tracks", "Game.bin")
    _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "tracks" / "Game.m3u"
    playlist.write_text("Game.bin\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"),
        {
            str(tmp_path / "Game.cue"): str(tmp_path / "Game.chd"),
            str(tmp_path / "tracks" / "Game.bin"): str(tmp_path / "Game.chd"),
        },
    )

    assert outcome.ok
    assert playlist.read_text() == "../Game.chd\n"


@pytest.mark.asyncio
async def test_a_new_entry_starting_with_hash_is_not_a_comment(tmp_path):
    """An output folder named #CHD must not turn the entry into a comment line."""
    _disc(tmp_path, "Game.cue")
    (tmp_path / "#CHD").mkdir()
    out = _disc(tmp_path / "#CHD", "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    assert outcome.ok, outcome.reason
    assert playlist.read_text() == "./#CHD/Game.chd\n"


@pytest.mark.asyncio
async def test_a_new_entry_starting_with_a_space_survives_trimming(tmp_path):
    """m3u readers trim entries, so a leading space must be protected by ./"""
    _disc(tmp_path, "Game.cue")
    (tmp_path / " CHD").mkdir()
    out = _disc(tmp_path / " CHD", "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    assert outcome.ok, outcome.reason
    assert playlist.read_text() == "./ CHD/Game.chd\n"


@pytest.mark.asyncio
async def test_a_new_entry_ending_in_a_space_keeps_the_source(tmp_path):
    """A trailing space can't be protected, so the Source is kept."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd ")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    assert not outcome.ok
    assert playlist.read_text() == "Game.cue\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["CHD\nextra", "CHD\rextra"])
async def test_an_output_path_with_a_line_break_keeps_the_source(tmp_path, folder):
    """A path that can't be one playlist line is never written."""
    _disc(tmp_path, "Game.cue")
    (tmp_path / folder).mkdir()
    out = _disc(tmp_path / folder, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    assert not outcome.ok
    assert playlist.read_text() == "Game.cue\n"


AWKWARD_NAMES = [
    " CHD", "CHD ", "#CHD", ";CHD", "\tCHD", "CHD\t", "a#b", "Pokémon",
    unicodedata.normalize("NFD", "Pokémon"), "CHD\nextra", "CHD\rextra",
    "CHD\x0bextra", "CHD\x1fextra", " ", "#",
    "﻿CHD", "a\\b", "\\CHD", "CHD\\", "CHD|converted", "|CHD",
]


def _reader_entries(data: bytes) -> list[str]:
    """What an emulator reads from a playlist, written out independently of
    the code under test and modelled on a C byte reader (RetroArch): a leading
    UTF-8 BOM dropped, lines split on CR/LF bytes only, each trimmed of ASCII
    space, tab, CR and LF only (never Unicode spaces), blank and # lines
    skipped, a ``path|label`` reduced to its path, surrounding quotes removed,
    and an entry with backslashes but no slash read as backslash-separated."""
    data = data.removeprefix(b"\xef\xbb\xbf")
    entries = []
    for raw in data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
        entry = raw.decode("utf-8").strip(" \t\r\n\v\f")
        if not entry or entry.startswith("#"):
            continue
        entry = entry.split("|", 1)[0].strip(" \t\r\n\v\f")
        if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in "\"'":
            entry = entry[1:-1]
        if "\\" in entry and "/" not in entry:
            entry = entry.replace("\\", "/")
        entries.append(entry)
    return entries


AWKWARD_SOURCE_NAMES = [
    "　Game.cue", "\xa0Game.cue", " Game.cue", " Game.cue",
    "\x1cGame.cue", "Game.cue\xa0", "Game　.cue",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", AWKWARD_SOURCE_NAMES, ids=repr)
async def test_awkward_source_names_round_trip_or_keep_the_source(tmp_path, source):
    """A Source whose name starts or ends with a Unicode space a byte reader
    keeps: the entry is either rewritten to read back to the output, or the
    Source is kept. Never left pointing at a deleted Source."""
    try:
        _disc(tmp_path, source)
    except OSError:
        pytest.skip("the filesystem refuses this name")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    original = f"{source}\nOther.cue\n".encode()
    playlist.write_bytes(original)
    assert os.path.samefile(tmp_path / _reader_entries(original)[0], tmp_path / source)

    outcome = await _replace(tmp_path, source, "Game.chd")

    if not outcome.ok:
        assert playlist.read_bytes() == original
        return
    entries = _reader_entries(playlist.read_bytes())
    assert os.path.samefile(tmp_path / entries[0], out)
    assert entries[1] == "Other.cue"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_line", [True, False], ids=["first-line", "after-header"])
@pytest.mark.parametrize("where", ["folder", "file"])
@pytest.mark.parametrize("name", AWKWARD_NAMES, ids=repr)
async def test_awkward_output_names_round_trip_or_keep_the_source(
    tmp_path, name, where, first_line,
):
    """Whatever the output (or its folder) is called, the playlist either reads
    back, as a reader would, to the output, or the Source is kept and the
    playlist is untouched."""
    _disc(tmp_path, "Game.cue")
    _disc(tmp_path, "Other.cue")
    try:
        if where == "folder":
            (tmp_path / name).mkdir()
            out = _disc(tmp_path / name, "Game.chd")
        else:
            out = _disc(tmp_path, f"{name}.chd")
    except OSError:
        pytest.skip("the filesystem refuses this name")
    playlist = tmp_path / "Game.m3u"
    original = ("" if first_line else "#EXTM3U\n") + "Game.cue\nOther.cue\n"
    playlist.write_text(original)

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    if not outcome.ok:
        assert playlist.read_text() == original
        return
    entries = _reader_entries(playlist.read_bytes())
    assert len(entries) == 2
    assert os.path.samefile(tmp_path / entries[0], out)
    assert entries[1] == "Other.cue"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["cp1252", "latin-1"])
@pytest.mark.parametrize("source", [
    "Pokémon.cue", "Café (Disc 1).cue", unicodedata.normalize("NFD", "Pokémon.cue"),
], ids=repr)
@pytest.mark.parametrize("entry_form", ["{}", "/userdata/roms/psx/{}", "{}|Disc 1"])
async def test_awkward_playlist_encodings_naming_the_source_keep_it(
    tmp_path, encoding, source, entry_form,
):
    """A playlist that isn't UTF-8 and names the Source is never rewritten,
    and the Source is kept."""
    _disc(tmp_path, source)
    _disc(tmp_path, "Other.cue")
    playlist = tmp_path / "P.m3u"
    entry = entry_form.format(unicodedata.normalize("NFC", source))
    data = f"#EXTM3U\nOther.cue\n{entry}\n".encode(encoding)
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, source, "Pokemon.chd")

    assert not outcome.ok
    assert "isn't UTF-8" in outcome.reason
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
async def test_a_non_utf8_playlist_naming_other_files_does_not_block(tmp_path):
    _disc(tmp_path, "Game.cue")
    playlist = tmp_path / "P.m3u"
    data = "Café.cue\n".encode("cp1252")
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
async def test_an_output_path_with_a_label_delimiter_keeps_the_source(tmp_path):
    """Readers split "path|label", and no spelling escapes the "|"."""
    _disc(tmp_path, "Game.cue")
    (tmp_path / "CHD|converted").mkdir()
    out = _disc(tmp_path / "CHD|converted", "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    )

    assert not outcome.ok
    assert playlist.read_text() == "Game.cue\n"


@pytest.mark.asyncio
async def test_a_first_entry_starting_with_a_bom_character_is_protected(tmp_path):
    """U+FEFF at the start of the file reads as a BOM, so it needs ./"""
    _disc(tmp_path, "Game.cue")
    _disc(tmp_path, "﻿Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await _replace(tmp_path, "Game.cue", "﻿Game.chd")

    assert outcome.ok, outcome.reason
    assert playlist.read_text() == "./﻿Game.chd\n"


@pytest.mark.asyncio
async def test_a_literal_backslash_in_a_name_is_protected(tmp_path):
    """A reader splits a slash-free entry on backslashes, so it needs ./"""
    _disc(tmp_path, "Game.cue")
    try:
        _disc(tmp_path, "Game\\ (Disc 2).chd")
    except OSError:
        pytest.skip("the filesystem refuses backslashes in names")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")

    outcome = await _replace(tmp_path, "Game.cue", "Game\\ (Disc 2).chd")

    assert outcome.ok, outcome.reason
    assert playlist.read_text() == "./Game\\ (Disc 2).chd\n"
    assert _reader_entries(playlist.read_bytes()) == ["./Game\\ (Disc 2).chd"]


@pytest.mark.parametrize(("name", "first_line", "expected"), [
    ("Game.iso", True, "Game.iso"),
    ("﻿Game.iso", True, "./﻿Game.iso"),
    ("﻿Game.iso", False, "﻿Game.iso"),
    ("Game\\ (Disc 2).iso", False, "./Game\\ (Disc 2).iso"),
    ("#Game.iso", False, "./#Game.iso"),
    (" Game.iso", False, "./ Game.iso"),
    ("Game.iso ", False, None),
    ("Game\n.iso", False, None),
])
def test_entry_for_new_playlists_uses_the_same_reader(tmp_path, name, first_line, expected):
    """The encoder shared with playlist creation (#299)."""
    try:
        _disc(tmp_path, name)
    except OSError:
        pytest.skip("the filesystem refuses this name")

    entry = m3u_module.entry_for(str(tmp_path), str(tmp_path / name), first_line=first_line)

    assert entry == expected
    if entry is not None:
        data = (entry + "\n").encode() if first_line else ("#EXTM3U\n" + entry + "\n").encode()
        [read] = _reader_entries(data)
        assert os.path.samefile(tmp_path / read, tmp_path / name)


@pytest.mark.asyncio
async def test_a_symlinked_playlist_is_never_read_and_keeps_the_source(
    tmp_path, tmp_path_factory, monkeypatch,
):
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.m3u"
    secret.write_text("Game.cue\n")
    _disc(tmp_path, "Game.cue")
    (tmp_path / "Game.m3u").symlink_to(secret)
    reads = []
    real_read = m3u_module._read
    monkeypatch.setattr(m3u_module, "_read", lambda p: reads.append(p) or real_read(p))

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert not outcome.ok
    assert "symlink" in outcome.reason
    assert reads == []
    assert secret.read_text() == "Game.cue\n"


@pytest.mark.asyncio
async def test_the_backup_keeps_the_playlists_mode(tmp_path):
    _disc(tmp_path, "A.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    playlist.chmod(0o600)

    outcome = await _replace(tmp_path, "A.cue", "A.chd")

    assert outcome.ok
    assert stat.S_IMODE((tmp_path / "Game.m3u.bak").stat().st_mode) == 0o600


def test_a_backup_whose_model_vanished_is_not_written(tmp_path):
    """If the playlist the backup copies its mode from is gone, the write
    fails (and the Source is kept) instead of falling back to 0644."""
    backup = tmp_path / "Game.m3u.bak"

    with pytest.raises(FileNotFoundError):
        atomic_write_bytes(str(backup), b"A.cue\n", like=str(tmp_path / "Game.m3u"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_the_backup_keeps_the_playlists_group(tmp_path):
    _disc(tmp_path, "A.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    group = _other_group(tmp_path)
    if group is None:
        pytest.skip("needs a second group")
    try:
        os.chown(playlist, -1, group)
    except PermissionError:
        pytest.skip("can't change group here")

    outcome = await _replace(tmp_path, "A.cue", "A.chd")

    assert outcome.ok
    assert (tmp_path / "Game.m3u.bak").stat().st_gid == group


def _other_group(path: Path) -> int | None:
    current = path.stat().st_gid
    return next((g for g in os.getgroups() if g != current), None)


def test_a_rewrite_keeps_the_playlists_group(tmp_path):
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    group = _other_group(tmp_path)
    if group is None:
        pytest.skip("needs a second group")
    try:
        os.chown(playlist, -1, group)
    except PermissionError:
        pytest.skip("can't change group here")

    atomic_write_bytes(str(playlist), b"A.chd\n")

    assert playlist.stat().st_gid == group


def _pretend_group_differs(monkeypatch, playlist: Path) -> None:
    """The playlist reports a group the temp file won't get, and chown refuses."""
    real_stat = os.stat
    original_gid = real_stat(playlist).st_gid

    def stat_with_other_group(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if str(path) == str(playlist):
            fields = list(st)
            fields[stat.ST_GID] = original_gid + 1
            return os.stat_result(fields)
        return st

    def refuse(*_a, **_kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(base_module.os, "stat", stat_with_other_group)
    monkeypatch.setattr(base_module.os, "chown", refuse)


def test_a_group_that_cannot_be_kept_fails_the_write(tmp_path, monkeypatch):
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    _pretend_group_differs(monkeypatch, playlist)

    with pytest.raises(PermissionError):
        atomic_write_bytes(str(playlist), b"A.chd\n")
    monkeypatch.undo()
    assert playlist.read_text() == "A.cue\n"
    assert [p.name for p in tmp_path.iterdir()] == ["Game.m3u"]


@pytest.mark.asyncio
async def test_a_group_that_cannot_be_kept_keeps_the_source(tmp_path, monkeypatch):
    _disc(tmp_path, "A.cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    (tmp_path / "Game.m3u.bak").write_text("A.cue\n")  # only the playlist is rewritten
    _pretend_group_differs(monkeypatch, playlist)

    outcome = await _replace(tmp_path, "A.cue", "A.chd")

    monkeypatch.undo()
    assert not outcome.ok
    assert "group" in outcome.reason
    assert playlist.read_text() == "A.cue\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"Game (Disc 1).cue\nGame (Disc 2).cue\x00",      # C-string terminator, even length
    b"Game (Disc 1).cue\nGame (Disc 2).cue\n\x00",    # odd length
    b"Game (Disc 1).cue\nX\x00\n",
], ids=["even", "odd", "mid"])
async def test_a_stray_nul_in_an_8_bit_playlist_keeps_the_source(tmp_path, data):
    """One NUL doesn't make a file UTF-16. A file with a NUL that isn't
    UTF-16 is undecodable; if any decoding names the Source it is kept."""
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert not outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("pad", [512, 4096, 513, 8])
async def test_a_nul_padded_8_bit_playlist_keeps_the_source(tmp_path, pad):
    """A zero-filled tail (an interrupted write on FAT, a preallocating sync)
    can fill one byte parity with NULs. The 8-bit readings still name the
    Source, so it is kept whatever the length."""
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    data = b"Game (Disc 1).cue\nGame (Disc 2).cue\n" + b"\0" * pad
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert not outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["A|B.cue", '"Game.cue"', "'Game.cue'"], ids=repr)
async def test_a_source_named_like_a_label_or_quotes_keeps_it(tmp_path, name):
    """A plain line reader reaches a Source literally named ``A|B.cue`` or
    ``"Game.cue"``; the label/quote reading doesn't. Keep the Source."""
    try:
        _disc(tmp_path, name)
    except OSError:
        pytest.skip("the filesystem refuses this name")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text(f"{name}\n")

    outcome = await _replace(tmp_path, name, "Game.chd")

    assert not outcome.ok
    assert playlist.read_text() == f"{name}\n"


@pytest.mark.asyncio
async def test_a_quoted_source_name_with_a_label_keeps_it(tmp_path):
    """RetroArch splits the label but keeps quotes: ``"Game.cue"|Disc 1``
    reaches a Source literally named ``"Game.cue"``."""
    try:
        _disc(tmp_path, '"Game.cue"')
    except OSError:
        pytest.skip("the filesystem refuses this name")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text('"Game.cue"|Disc 1\n')

    outcome = await _replace(tmp_path, '"Game.cue"', "Game.chd")

    assert not outcome.ok
    assert playlist.read_text() == '"Game.cue"|Disc 1\n'


@pytest.mark.asyncio
async def test_a_utf16_file_with_an_appended_8_bit_line_keeps_the_source(tmp_path):
    """A Notepad "Unicode" playlist with an 8-bit line appended: line readers
    read that line cleanly, so its mention keeps the Source despite the BOM."""
    _disc(tmp_path, "Game (Disc 1).cue")
    playlist = tmp_path / "Game.m3u"
    data = "Other.cue\n".encode("utf-16") + b"\nGame (Disc 1).cue\n"
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert not outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("name", [
    "日本語日本語日本語.cue", "Game 日本語日本語日本語日本語.cue",
], ids=["cjk", "cjk+ascii"])
async def test_a_bomless_utf16_playlist_with_few_nuls_keeps_the_source(
    tmp_path, encoding, name,
):
    """Long CJK names have too few NULs for the parity guess; both UTF-16
    readings are still checked whenever there is any NUL."""
    _disc(tmp_path, name)
    playlist = tmp_path / "P.m3u"
    data = f"{name}\n".encode(encoding)
    playlist.write_bytes(data)

    outcome = await _replace(tmp_path, name, "Game.chd")

    assert not outcome.ok
    assert playlist.read_bytes() == data


@pytest.mark.asyncio
async def test_a_zero_padded_playlist_is_not_called_utf16(tmp_path):
    _disc(tmp_path, "Game (Disc 1).cue")
    (tmp_path / "Game.m3u").write_bytes(b"Game (Disc 1).cue\n" + b"\0" * 512)

    outcome = await _replace(tmp_path, "Game (Disc 1).cue", "Game (Disc 1).chd")

    assert not outcome.ok
    assert "UTF-16" not in outcome.reason
    assert "NUL bytes" in outcome.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("space", ["\x0b", "\x0c"], ids=["VT", "FF"])
async def test_vertical_tab_and_form_feed_are_trimmed_like_retroarch(tmp_path, space):
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_bytes(f"{space}Game.cue\n".encode())

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok, outcome.reason
    assert playlist.read_bytes() == f"{space}Game.chd\n".encode()
    [read] = _reader_entries(playlist.read_bytes())
    assert os.path.samefile(tmp_path / read, out)


@pytest.mark.asyncio
async def test_a_stray_nul_in_an_unrelated_playlist_does_not_block(tmp_path):
    _disc(tmp_path, "Game.cue")
    other = tmp_path / "Other.m3u"
    other.write_bytes(b"Other.cue\n\x00")

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok
    assert other.read_bytes() == b"Other.cue\n\x00"


def _unstable_inodes(monkeypatch, root: Path) -> None:
    """Simulate a FUSE/SMB mount (Unraid's shfs user shares) where st_ino can
    change between two stats of the same file."""
    import itertools

    real_stat = os.stat
    counter = itertools.count(10_000_000)
    root_real = os.path.realpath(root)

    def unstable_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if not stat.S_ISREG(st.st_mode) or not os.path.realpath(path).startswith(root_real):
            return st
        fields = list(st)
        fields[stat.ST_INO] = next(counter)
        return os.stat_result(fields, {
            "st_atime_ns": st.st_atime_ns,
            "st_mtime_ns": st.st_mtime_ns,
            "st_ctime_ns": st.st_ctime_ns,
        })

    monkeypatch.setattr(os, "stat", unstable_stat)


@pytest.mark.asyncio
async def test_unstable_inodes_on_a_fuse_share_still_rewrite(tmp_path, monkeypatch):
    """On shfs the same file can report a new st_ino on every stat. Matching
    falls back to (realpath, size, mtime), so the playlist is still rewritten
    and the output check still passes."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\nOther.cue\n")
    _unstable_inodes(monkeypatch, tmp_path)

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    monkeypatch.undo()
    assert outcome.ok, outcome.reason
    assert playlist.read_text() == "Game.chd\nOther.cue\n"
    assert os.path.samefile(tmp_path / _reader_entries(playlist.read_bytes())[0], out)


@pytest.mark.asyncio
async def test_unstable_inodes_still_catch_a_replaced_output(tmp_path, monkeypatch):
    """The fallback still sees a different disc written at the same path."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")
    _unstable_inodes(monkeypatch, tmp_path)
    entered, release, _finished = _stall_reads(monkeypatch)

    hook = asyncio.create_task(referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        _replace_output(out)
    finally:
        release.set()
    outcome = await asyncio.wait_for(hook, timeout=10)

    monkeypatch.undo()
    assert not outcome.ok
    assert playlist.read_text() == "Game.cue\n"


def test_an_output_rewritten_in_place_fails_the_write_guard(tmp_path):
    """Same inode, new bytes (a conversion opening it with O_TRUNC): the
    output is no longer the one checked, so nothing may point at it."""
    import threading

    out = tmp_path / "Game.chd"
    out.write_bytes(b"checked output")
    guard = base_module.WriteGuard(
        abandoned=threading.Event(), outputs={str(out): file_identity(str(out))},
    )
    inode = out.stat().st_ino
    with open(out, "r+b") as f:
        f.truncate(0)
        f.write(b"something else entirely")
    os.utime(out, ns=(1_000_000_000, 1_000_000_000))

    assert out.stat().st_ino == inode
    with pytest.raises(referrers.ReferrerError):
        guard.check(str(tmp_path / "Game.m3u"))


@pytest.mark.parametrize("differs", ["size", "mtime_ns"])
def test_a_reused_inode_with_different_content_is_not_the_same_file(differs):
    """Unstable inodes can collide: matching (dev, ino) alone isn't enough."""
    a = FileIdentity(dev=1, ino=42, size=100, mtime_ns=5, realpath="/vol/a.cue")
    b = a._replace(realpath="/vol/b.cue", **{differs: getattr(a, differs) + 1})

    assert not same_identity(a, b)
    assert same_identity(a, a._replace(ino=43))  # shfs: new inode, same file
    assert not same_identity(a, a._replace(ino=43, realpath="/vol/c.cue"))


def _stall_reads(monkeypatch):
    """Block the first playlist read; signal entry and the worker's end."""
    import threading

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_read, real_update = m3u_module._read, referrers._update_sync
    blocked = []

    def stuck_read(path):
        if not blocked:
            blocked.append(path)
            entered.set()
            release.wait(30)
        return real_read(path)

    def update_then_signal(*args, **kwargs):
        try:
            return real_update(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(m3u_module, "_read", stuck_read)
    monkeypatch.setattr(referrers, "_update_sync", update_then_signal)
    return entered, release, finished


def _replace_output(path: Path) -> None:
    """Another conversion writes a different disc at the same path."""
    path.unlink()
    path.write_bytes(b"a different disc")


@pytest.mark.asyncio
async def test_an_abandoned_hook_never_writes_after_cancel(tmp_path, monkeypatch):
    """Codex's sequence: the hook stalls on a read, the job cancels it and
    lets go of the output, another job replaces the output, the read returns.
    The late worker must not touch the playlist."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")
    entered, release, finished = _stall_reads(monkeypatch)
    cancel = asyncio.Event()

    hook = asyncio.create_task(referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
        cancel_event=cancel,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        cancel.set()
        outcome = await asyncio.wait_for(hook, timeout=5)
        _replace_output(out)
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 10), "the late worker didn't finish"

    assert not outcome.ok
    assert playlist.read_text() == "Game.cue\n"
    assert not (tmp_path / "Game.m3u.bak").exists()


@pytest.mark.asyncio
async def test_an_output_replaced_during_the_hook_is_never_pointed_at(tmp_path, monkeypatch):
    """Even before any timeout: the output must still be the one verified
    (same device, inode, size, mtime) when the playlist is written."""
    _disc(tmp_path, "Game.cue")
    out = _disc(tmp_path, "Game.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("Game.cue\n")
    entered, release, _finished = _stall_reads(monkeypatch)

    hook = asyncio.create_task(referrers.update_before_source_removal(
        str(tmp_path / "Game.cue"), {str(tmp_path / "Game.cue"): str(out)},
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        _replace_output(out)
    finally:
        release.set()
    outcome = await asyncio.wait_for(hook, timeout=10)

    assert not outcome.ok
    assert "output" in outcome.reason
    assert playlist.read_text() == "Game.cue\n"


def test_apply_refuses_when_the_output_is_gone(tmp_path):
    """A late (abandoned) apply must not point a playlist at a removed output."""
    _disc(tmp_path, "A.cue")
    out = _disc(tmp_path, "A.chd")
    playlist = tmp_path / "Game.m3u"
    playlist.write_text("A.cue\n")
    m3u = M3uReferrer()
    [change] = m3u.plan({str(tmp_path / "A.cue"): str(out)})

    out.unlink()

    with pytest.raises(referrers.ReferrerError):
        m3u.apply(change)
    assert playlist.read_text() == "A.cue\n"


@pytest.mark.asyncio
async def test_an_unrelated_utf16_playlist_does_not_block(tmp_path):
    _disc(tmp_path, "Game.cue")
    other = tmp_path / "Other.m3u"
    other.write_bytes("Other.cue\n".encode("utf-16"))

    outcome = await _replace(tmp_path, "Game.cue", "Game.chd")

    assert outcome.ok
    assert other.read_bytes() == "Other.cue\n".encode("utf-16")


@pytest.mark.asyncio
async def test_nested_volumes_walk_to_the_outer_root(tmp_path, monkeypatch):
    outer = tmp_path / "roms"
    inner = outer / "psx"
    (inner / ".discs").mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", f"{outer},{inner}")
    _disc(inner / ".discs", "G.cue")
    top = outer / "All.m3u"
    top.write_text("psx/.discs/G.cue\n")

    outcome = await _replace(inner / ".discs", "G.cue", "G.chd")

    assert outcome.ok
    assert top.read_text() == "psx/.discs/G.chd\n"


@pytest.mark.asyncio
async def test_an_nfd_spelling_of_the_source_name_keeps_it(tmp_path):
    name_nfc = unicodedata.normalize("NFC", "Pokémon.cue")
    name_nfd = unicodedata.normalize("NFD", "Pokémon.cue")
    _disc(tmp_path, name_nfc)
    playlist = tmp_path / "P.m3u"
    playlist.write_text(f"/userdata/roms/psx/{name_nfd}\n")

    outcome = await _replace(tmp_path, name_nfc, "Pokemon.chd")

    assert not outcome.ok
    assert "line 1" in outcome.kept_message
