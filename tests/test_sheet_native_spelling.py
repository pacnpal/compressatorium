"""A sheet's tracks are the files chdman opens: the raw-byte spelling (Codex 4576999696).

chdman opens a track by the sheet's raw bytes. For a sheet that is not UTF-8,
the decoded spellings (cp1252, Latin-1) name a *different* file on POSIX
(``é`` re-encoded as UTF-8), so a delete plan built from them could delete an
unrelated file after the conversion consumed the raw-byte one. So:

(a) deletion targets come from the native spelling (``os.fsdecode`` of the
    raw bytes), never from a decoded reading;
(b) when a decoded spelling and the native one name different existing files,
    the sheet is ambiguous: unreadable for every reader (so the job gate built
    on ``sheet_readings`` refuses it), refused by the delete plan, nothing
    deleted.

POSIX only: these need a file name that is not valid UTF-8, which macOS
refuses; on Linux they run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.sheet_text import sheet_readings, sheet_track_list

_RAW = b"\xe9.bin"                       # what chdman opens for FILE "\xe9.bin"
_UTF8 = "é.bin"                      # the cp1252/Latin-1 reading, as a file name
_SHEET = b'FILE "' + _RAW + b'" BINARY\n  TRACK 01 MODE1/2352\n'


@pytest.fixture(name="folder")
def _folder(tmp_path, monkeypatch):
    from config import settings

    folder = tmp_path / "volume" / "games"
    folder.mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path / "volume"))
    try:
        probe = os.path.join(os.fsencode(folder), b"probe-\xff")
        with open(probe, "wb"):
            pass
        os.remove(probe)
    except OSError:
        pytest.skip("this filesystem refuses non-UTF-8 file names (macOS); runs on Linux")
    return folder


def _raw_file(folder: Path, data: bytes = b"raw track") -> str:
    path = os.path.join(os.fsencode(folder), _RAW)
    with open(path, "wb") as fh:
        fh.write(data)
    return os.fsdecode(path)


def _utf8_file(folder: Path, data: bytes = b"unrelated file") -> Path:
    path = folder / _UTF8
    path.write_bytes(data)
    return path


def _sheet(folder: Path) -> str:
    cue = folder / "game.cue"
    cue.write_bytes(_SHEET)
    return str(cue)


def test_codex_repro_two_files_make_the_sheet_ambiguous_and_nothing_is_deleted(folder):
    raw = _raw_file(folder)
    unrelated = _utf8_file(folder)
    cue = _sheet(folder)

    with pytest.raises(ValueError, match="(?i)ambiguous"):
        build_delete_snapshot(cue)
    plan = build_delete_plan(cue)
    assert any("ambiguous" in e.lower() for e in plan["errors"]), plan
    assert str(unrelated) not in plan["delete_paths"]
    assert sheet_readings(cue) == []          # every reader, the job gate included
    assert sheet_track_list(cue) is None
    assert unrelated.read_bytes() == b"unrelated file"
    assert Path(raw).read_bytes() == b"raw track"


def test_the_delete_plan_targets_the_raw_byte_file_chdman_opens(folder):
    raw = _raw_file(folder)
    cue = _sheet(folder)

    plan = build_delete_plan(cue)

    assert not plan["errors"] and not plan["unsafe_paths"], plan
    assert os.path.realpath(raw) in plan["delete_paths"]
    build_delete_snapshot(cue)                # accepted


def test_a_decoded_spelling_alone_is_never_a_deletion_target(folder):
    # Only the UTF-8 file exists: chdman (raw bytes) would not find its track,
    # and the plan must not substitute the decoded spelling's file for it.
    unrelated = _utf8_file(folder)
    cue = _sheet(folder)

    plan = build_delete_plan(cue)

    assert str(unrelated) not in plan["delete_paths"]
    assert plan["missing_paths"], plan
    with pytest.raises(ValueError):
        build_delete_snapshot(cue)
