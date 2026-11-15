"""The one CUE FILE-line parser (``utils.sheet_text.cue_tracks``)."""
from __future__ import annotations

import pytest
from utils.sheet_text import cue_tracks


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ('FILE "track.bin" BINARY', "track.bin"),
        ('FILE "Game (Track 1).bin" BINARY', "Game (Track 1).bin"),
        ("FILE track.bin BINARY", "track.bin"),
        ("FILE Game (Track 1).bin BINARY", "Game (Track 1).bin"),
        ('  file "lower.bin" binary', "lower.bin"),
        ("FILE track.bin", "track.bin"),
    ],
    ids=["quoted", "quoted-spaces", "unquoted", "unquoted-spaces", "lowercase", "no-type"],
)
def test_a_file_line_gives_the_whole_name(line, name):
    assert cue_tracks(f"{line}\n  TRACK 01 MODE1/2352\n") == [name]


def test_rem_lines_and_other_commands_are_not_tracks():
    sheet = (
        'REM FILE "not-a-track.bin" BINARY\n'
        'FILE "one.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
        "FILE two.bin BINARY\n  TRACK 02 AUDIO\n"
        "FILENAME oops\n"
    )

    assert cue_tracks(sheet) == ["one.bin", "two.bin"]


# --------------------------------------------------------------------------- #
# Oversized sheets: unreadable, never a partial parse
# --------------------------------------------------------------------------- #

def _sheet_with_padding(folder, *, pad_to: int, after: str) -> str:
    """A cue naming one.bin, REM-padded to ``pad_to`` bytes, then ``after``."""
    head =b'FILE "one.bin" BINARY\n  TRACK 01 MODE1/2352\n'
    tail = after.encode()
    filler_line = b"REM " + b"x" * 60 + b"\n"
    body = bytearray(head)
    while len(body) + len(filler_line) + len(tail) <= pad_to:
        body += filler_line
    body += b"REM " + b"y" * max(0, pad_to - len(body) - len(tail) - 5) + b"\n"
    body += tail
    path = folder / "game.cue"
    path.write_bytes(bytes(body))
    (folder / "one.bin").write_bytes(b"\0" * 2352)
    (folder / "two.bin").write_bytes(b"\0" * 2352)
    return str(path)


def test_a_sheet_over_the_cap_is_unreadable(tmp_path):
    from utils.sheet_text import MAX_SHEET_BYTES, sheet_readings

    path = _sheet_with_padding(
        tmp_path, pad_to=MAX_SHEET_BYTES + 1, after='FILE "two.bin" BINARY\n',
    )
    assert (tmp_path / "game.cue").stat().st_size > MAX_SHEET_BYTES

    assert sheet_readings(path) == []


def test_a_sheet_exactly_at_the_cap_still_parses(tmp_path):
    from utils.sheet_text import MAX_SHEET_BYTES, sheet_readings

    path = _sheet_with_padding(
        tmp_path, pad_to=MAX_SHEET_BYTES, after='FILE "two.bin" BINARY\n',
    )
    assert (tmp_path / "game.cue").stat().st_size == MAX_SHEET_BYTES

    assert cue_tracks(sheet_readings(path)[0]) == ["one.bin", "two.bin"]


def test_delete_snapshot_refuses_an_oversized_cue_with_a_track_past_the_cap(tmp_path):
    from utils.delete_plan import build_delete_snapshot
    from utils.sheet_text import MAX_SHEET_BYTES

    path = _sheet_with_padding(
        tmp_path, pad_to=MAX_SHEET_BYTES + 64, after='FILE "two.bin" BINARY\n',
    )

    with pytest.raises(ValueError):
        build_delete_snapshot(path)


def test_an_oversized_cue_lists_no_partial_tracks_and_is_not_cached(tmp_path, monkeypatch):
    from services import platform_detect
    from services.tools import registry
    from utils.delete_plan import sheet_track_paths
    from utils.sheet_text import MAX_SHEET_BYTES

    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    path = _sheet_with_padding(
        tmp_path, pad_to=MAX_SHEET_BYTES + 64, after='FILE "two.bin" BINARY\n',
    )
    platform_detect.clear_cache()

    assert sheet_track_paths(path) is None            # unknown, not "no tracks"
    assert registry.get("chdman").header_reads(path) is None
    platform_detect.detect_platform(path)
    assert path not in platform_detect._cache
    platform_detect.clear_cache()
