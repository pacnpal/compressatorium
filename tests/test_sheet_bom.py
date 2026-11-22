"""A UTF-8 BOM is part of what chdman reads.

chdman 0.285 does not strip a BOM: a BOM-prefixed ``FILE "unrelated.bin"``
on the first line tokenizes as ``\\ufeffFILE``, so chdman ignores it and
converts from the tracks that follow. The decoded readings strip the BOM and
see that command. The native reading (the bytes chdman reads) keeps the BOM,
and when the readings disagree on how many FILE references the sheet has, the
sheet is ambiguous and refused: the delete plan never lists ``unrelated.bin``.
A BOM before a ``REM`` line changes nothing and stays readable.
"""
from __future__ import annotations

import pytest
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.sheet_text import sheet_readings, sheet_track_list

_BOM = b"\xef\xbb\xbf"


@pytest.fixture(name="folder")
def _folder(tmp_path, monkeypatch):
    from config import settings

    folder = tmp_path / "volume" / "games"
    folder.mkdir(parents=True)
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path / "volume"))
    (folder / "unrelated.bin").write_bytes(b"not a track")
    (folder / "game.bin").write_bytes(b"\0" * 2352)
    return folder


def test_a_bom_hidden_file_command_is_never_a_deletion_target(folder):
    cue = folder / "game.cue"
    cue.write_bytes(
        _BOM + b'FILE "unrelated.bin" BINARY\n'
        b'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n',
    )

    with pytest.raises(ValueError, match="(?i)ambiguous|cannot read"):
        build_delete_snapshot(str(cue))
    plan = build_delete_plan(str(cue))
    assert str(folder / "unrelated.bin") not in plan["delete_paths"]
    assert sheet_readings(cue) == []
    assert sheet_track_list(cue) is None
    assert (folder / "unrelated.bin").exists()


def test_a_bom_before_a_rem_line_stays_readable(folder):
    cue = folder / "game.cue"
    cue.write_bytes(
        _BOM + b'REM GENRE "Action"\n'
        b'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n',
    )

    plan = build_delete_plan(str(cue))

    assert not plan["errors"] and not plan["unsafe_paths"], plan
    assert str(folder / "game.bin") in plan["delete_paths"]
    assert str(folder / "unrelated.bin") not in plan["delete_paths"]
    assert sheet_track_list(cue) == [folder / "game.bin"]
