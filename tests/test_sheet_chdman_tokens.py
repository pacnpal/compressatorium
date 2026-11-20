"""Sheets are read the way chdman 0.285 reads them, so no track can hide.

chdman opens the files a ``.cue`` / ``.gdi`` names itself
(``src/lib/util/cdrom.cpp`` at mame0285). If our reading of a sheet differs
from its tokenizer, a name can pass ``resolve_track`` while chdman opens a
different one. So ``utils.sheet_text`` reproduces it exactly:

* lines are read with ``fgets(buf, 511)``: at most 510 bytes per read, split on
  ``\\n`` only. A longer line would be read in pieces, each parsed as a line
  (a ``REM`` padded past 510 bytes hides a ``FILE``), so a sheet with one is
  unreadable, never partly parsed. A NUL byte ends chdman's token early, so a
  sheet with one is unreadable too;
* ``tokenize``: C-locale whitespace separates tokens; ``"`` and ``'`` each
  toggle a quoted run (the other kind is literal inside it) and are dropped;
  there is no comment syntax (``#`` is filename text);
* a CUE track is the token after a ``FILE`` token (case-sensitive); a GDI
  track is the fifth token of a track line;
* the name chdman opens is the sheet's folder plus that token, verbatim: no
  quote or space stripping, and ``\\`` is a filename character on POSIX.

Every spelling of every name, including the raw-byte one chdman opens, goes
through ``resolve_track``; one refused spelling refuses the sheet.
"""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest
from services import platform_detect
from services.platform_detect import detect_platform
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.sheet_text import (
    chd_tokens,
    cue_tracks,
    gdi_tracks,
    sheet_readings,
    sheet_track_list,
)


def _saturn_track(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sector = bytearray(2352)
    sector[16:16 + 16] = b"SEGA SEGASATURN "
    path.write_bytes(bytes(sector))
    return path


@pytest.fixture(name="layout")
def _layout(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    folder = volume / "games"
    folder.mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    platform_detect.clear_cache()
    # `../../x.bin` from the sheet's folder: outside the Volume.
    outside = _saturn_track(tmp_path / "x.bin")
    yield {"volume": volume, "folder": folder, "outside": outside, "tmp": tmp_path}
    platform_detect.clear_cache()


@pytest.fixture(name="opened")
def _opened(monkeypatch):
    seen: list[str] = []
    real_open = builtins.open
    real_io_open = io.open

    def spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    def io_spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_io_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", io_spy)
    return seen


def _was_opened(seen: list[str], target: Path) -> bool:
    real = Path(os.path.realpath(target))
    return any(Path(os.path.realpath(p)) == real for p in seen if p)


def _assert_refused_and_never_opened(sheet: Path, outside: Path, opened: list[str]) -> None:
    opened.clear()
    # The job's gate for delete-on-verify, the track list the worker and the
    # cache read, and the Platform probe: each refuses, none opens the track.
    with pytest.raises(ValueError):
        build_delete_snapshot(str(sheet))
    plan = build_delete_plan(str(sheet))
    assert plan["unsafe_paths"] or plan["errors"], plan
    assert str(outside) not in plan["delete_paths"]
    assert sheet_track_list(sheet) is None
    assert detect_platform(str(sheet)).platform is None
    assert not _was_opened(opened, outside)


# --------------------------------------------------------------------------- #
# The tokenizer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("line", "tokens"),
    [
        ('FILE "Game (Track 1).bin" BINARY', ["FILE", "Game (Track 1).bin", "BINARY"]),
        ("FILE '../../x.bin' BINARY", ["FILE", "../../x.bin", "BINARY"]),
        ("FILE Game (Track 1).bin BINARY", ["FILE", "Game", "(Track", "1).bin", "BINARY"]),
        ("""FILE "it's.bin" BINARY""", ["FILE", "it's.bin", "BINARY"]),
        ("""FILE 'say "hi".bin' BINARY""", ["FILE", 'say "hi".bin', "BINARY"]),
        ('FILE a"b c"d BINARY', ["FILE", "ab cd", "BINARY"]),
        ('FILE "" BINARY', ["FILE", "", "BINARY"]),
        ("1 0 4 2048 #link/x.bin 0", ["1", "0", "4", "2048", "#link/x.bin", "0"]),
        ("\tFILE\tx.bin\tBINARY\r", ["FILE", "x.bin", "BINARY"]),
        ('FILE "x.bin ', ["FILE", "x.bin "]),
        ("   ", []),
    ],
    ids=[
        "double-quoted", "single-quoted", "unquoted-spaces-split", "apostrophe-in-double",
        "double-in-single", "quotes-mid-token", "empty-quoted", "hash-is-text", "tabs-cr",
        "unclosed-quote-runs-to-end", "blank",
    ],
)
def test_tokens_match_chdman(line, tokens):
    assert chd_tokens(line) == tokens


def test_file_is_case_sensitive_and_rem_hides_nothing_on_one_read():
    sheet = (
        'REM FILE "not-a-track.bin" BINARY\n'
        'file "lower.bin" BINARY\n'
        'FILE "one.bin" BINARY\n  TRACK 01 MODE2/2352\n'
        "FILENAME oops\n"
    )

    assert cue_tracks(sheet) == ["one.bin"]


def test_a_gdi_hash_name_is_a_name():
    tracks = gdi_tracks("1\n1 0 4 2048 #link/x.bin 0\n")

    assert [(t.number, t.sector_size, t.name) for t in tracks] == [("1", 2048, "#link/x.bin")]


# --------------------------------------------------------------------------- #
# The three payloads: refused, never opened
# --------------------------------------------------------------------------- #

def test_a_rem_line_padded_past_chdmans_read_size_makes_the_sheet_unreadable(
    layout, opened,
):
    sheet = layout["folder"] / "game.cue"
    # chdman reads 510 bytes, then parses the rest of the line as its own line.
    line = b"REM " + b" " * 506 + b'FILE "../../x.bin" BINARY\n'
    sheet.write_bytes(line + b"  TRACK 01 MODE1/2352\n")

    assert sheet_readings(sheet) == []
    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


def test_a_single_quoted_dotdot_track_is_refused(layout, opened):
    sheet = layout["folder"] / "game.cue"
    sheet.write_text("FILE '../../x.bin' BINARY\n  TRACK 01 MODE1/2352\n")

    assert cue_tracks(sheet.read_text()) == ["../../x.bin"]
    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


def test_a_gdi_hash_track_through_a_symlink_out_is_refused(layout, opened):
    link = layout["folder"] / "#link"
    link.symlink_to(layout["tmp"], target_is_directory=True)
    sheet = layout["folder"] / "game.gdi"
    sheet.write_text("1\n1 0 4 2048 #link/x.bin 0\n")

    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


# --------------------------------------------------------------------------- #
# Names are checked verbatim
# --------------------------------------------------------------------------- #

def test_a_quoted_trailing_space_name_is_checked_as_written(layout, opened):
    # chdman opens "x.bin " (with the space); a symlink by that name leading out
    # must be refused, not confused with an in-Volume "x.bin".
    _saturn_track(layout["folder"] / "x.bin")
    (layout["folder"] / "x.bin ").symlink_to(layout["outside"])
    sheet = layout["folder"] / "game.cue"
    sheet.write_text('FILE "x.bin " BINARY\n  TRACK 01 MODE1/2352\n')

    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


def test_a_backslash_is_a_filename_character_on_posix(layout, opened):
    if os.sep == "\\":
        pytest.skip("on Windows the backslash is a separator")
    _saturn_track(layout["folder"] / "sub" / "x.bin")          # in the Volume
    (layout["folder"] / "sub\\x.bin").symlink_to(layout["outside"])   # what chdman opens
    sheet = layout["folder"] / "game.cue"
    sheet.write_text('FILE "sub\\x.bin" BINARY\n  TRACK 01 MODE1/2352\n')

    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


def test_the_raw_byte_spelling_chdman_opens_is_checked(layout, opened):
    # A cp1252 sheet: chdman opens the raw bytes, not the UTF-8 re-encoding of
    # either decoded spelling.
    raw_name = b"\xe9.bin"
    try:
        os.symlink(layout["outside"], os.path.join(os.fsencode(layout["folder"]), raw_name))
    except OSError:
        pytest.skip("this filesystem refuses non-UTF-8 file names")
    sheet = layout["folder"] / "game.cue"
    sheet.write_bytes(b'FILE "' + raw_name + b'" BINARY\n  TRACK 01 MODE1/2352\n')

    _assert_refused_and_never_opened(sheet, layout["outside"], opened)


def test_a_nul_byte_makes_the_sheet_unreadable(layout):
    sheet = layout["folder"] / "game.cue"
    sheet.write_bytes(b'FILE "ok.bin\x00../../x.bin" BINARY\n')

    assert sheet_readings(sheet) == []


# --------------------------------------------------------------------------- #
# Normal sheets still parse
# --------------------------------------------------------------------------- #

def test_normal_sheets_still_parse_and_detect(layout):
    folder = layout["folder"]
    _saturn_track(folder / "Game (Track 1).bin")
    (folder / "Game (Track 2).bin").write_bytes(b"\0" * 2352)
    cue = folder / "game.cue"
    cue.write_bytes(
        b'FILE "Game (Track 1).bin" BINARY\r\n  TRACK 01 MODE1/2352\r\n'
        b"    INDEX 01 00:00:00\r\n"
        b'FILE "Game (Track 2).bin" BINARY\r\n  TRACK 02 AUDIO\r\n'
    )
    gdi = folder / "game.gdi"
    gdi.write_text('2\n1 0 4 2352 "Game (Track 1).bin" 0\n2 600 0 2352 "Game (Track 2).bin" 0\n')

    assert sheet_track_list(cue) == [folder / "Game (Track 1).bin", folder / "Game (Track 2).bin"]
    assert sheet_track_list(gdi) == [folder / "Game (Track 1).bin", folder / "Game (Track 2).bin"]
    assert detect_platform(str(cue)).platform == "saturn"
    assert not build_delete_plan(str(cue))["unsafe_paths"]


def _line_of(length: int) -> tuple[str, str]:
    name = "a" * (length - len('FILE "" BINARY') - len(".bin")) + ".bin"
    line = f'FILE "{name}" BINARY'
    assert len(line.encode()) == length
    return line, name


def test_a_line_that_fits_one_chdman_read_still_parses(layout):
    line, name = _line_of(509)      # 509 bytes + "\n": one 510-byte read
    cue = layout["folder"] / "game.cue"
    cue.write_text(f"{line}\n  TRACK 01 MODE1/2352\n")

    assert cue_tracks(sheet_readings(cue)[0]) == [name]


def test_a_line_one_byte_over_a_chdman_read_is_unreadable(layout):
    line, _name = _line_of(510)     # 510 bytes + "\n": the newline is a second read
    cue = layout["folder"] / "game.cue"
    cue.write_text(f"{line}\n  TRACK 01 MODE1/2352\n")

    assert sheet_readings(cue) == []


# --------------------------------------------------------------------------- #
# Unterminated quotes
# --------------------------------------------------------------------------- #
# chdman's tokenizer keeps the line's "\n" inside an unclosed quote, so
# `1 0 4 2048 'pipe` opens a file named "pipe\n". A sheet with a line whose
# quote never closes is unreadable, for CUE and GDI alike. A CUE `REM` line is
# exempt: chdman reads no file name from one, and comments carry apostrophes.

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_gdi_unclosed_quote_naming_a_fifo_is_refused_and_never_opened(layout, opened):
    import threading

    fifo = layout["folder"] / "pipe\n"
    os.mkfifo(fifo)
    sheet = layout["folder"] / "game.gdi"
    sheet.write_bytes(b"1\n1 0 4 2048 'pipe\n")

    assert sheet_readings(sheet) == []
    with pytest.raises(ValueError):
        build_delete_snapshot(str(sheet))
    assert sheet_track_list(sheet) is None

    box: dict = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("result", detect_platform(str(sheet))), daemon=True,
    )
    worker.start()
    worker.join(3)
    if worker.is_alive():                      # unblock a stuck reader, then fail
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("detection opened the FIFO named by the unclosed quote")
    assert box["result"].platform is None
    assert not any(p.endswith("pipe\n") for p in opened)


@pytest.mark.parametrize(
    "sheet_name,body",
    [
        ("game.cue", b"FILE 'x.bin BINARY\n  TRACK 01 MODE1/2352\n"),
        ("game.cue", b'FILE "x.bin BINARY\n  TRACK 01 MODE1/2352\n'),
        ("game.cue", b'FILE "x.bin" BINARY\n  TRACK 01 MODE1/2352 "\n'),
        ("game.gdi", b'1\n1 0 4 2352 "x.bin 0\n'),
    ],
    ids=["cue-single", "cue-double", "cue-later-line", "gdi-double"],
)
def test_any_unclosed_quote_makes_the_sheet_unreadable(layout, sheet_name, body):
    sheet = layout["folder"] / sheet_name
    sheet.write_bytes(body)

    assert sheet_readings(sheet) == []


def test_an_apostrophe_in_a_rem_comment_is_still_readable(layout):
    sheet = layout["folder"] / "game.cue"
    sheet.write_bytes(
        b"REM COMMENT Don't panic\n"
        b'FILE "it\'s.bin" BINARY\n  TRACK 01 MODE1/2352\n',
    )

    assert cue_tracks(sheet_readings(sheet)[0]) == ["it's.bin"]
