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
