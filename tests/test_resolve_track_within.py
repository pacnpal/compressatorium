"""``resolve_track(within=...)``: confining tracks to roots other than the Volumes.

The job worker extracts an archived .cue/.gdi set into the job's temp dir,
which is not a Volume, so the job gate confines those tracks to the extraction
root instead. The default stays the configured Volumes.
"""
from __future__ import annotations

import pytest
from utils.sheet_text import resolve_track, sheet_tracks


@pytest.fixture(name="dirs")
def _dirs(tmp_path, monkeypatch):
    from config import settings

    volume = tmp_path / "volume"
    extract = tmp_path / "job-temp" / "extract"
    other = tmp_path / "other"
    for folder in (volume, extract, other):
        folder.mkdir(parents=True)
    (extract / "track.bin").write_bytes(b"\0")
    (other / "x.bin").write_bytes(b"\0")
    monkeypatch.setattr(settings, "chd_volumes", str(volume))
    return {"volume": volume, "extract": extract, "other": other}


def test_default_refuses_a_track_outside_the_volumes(dirs):
    with pytest.raises(ValueError):
        resolve_track(dirs["extract"], "track.bin")


def test_within_accepts_a_track_under_the_extraction_root(dirs):
    track = resolve_track(dirs["extract"], "track.bin", within=[dirs["extract"].parent])

    assert track == dirs["extract"] / "track.bin"


@pytest.mark.parametrize("name", ["/etc/passwd", "../../other/x.bin", "sub/../track.bin"])
def test_within_still_refuses_absolute_and_dotdot(dirs, name):
    with pytest.raises(ValueError):
        resolve_track(dirs["extract"], name, within=[dirs["extract"]])


def test_within_refuses_a_symlink_out_of_the_root(dirs):
    (dirs["extract"] / "escape.bin").symlink_to(dirs["other"] / "x.bin")

    with pytest.raises(ValueError):
        resolve_track(dirs["extract"], "escape.bin", within=[dirs["extract"]])


def test_within_never_accepts_an_empty_root_list(dirs):
    with pytest.raises(ValueError):
        resolve_track(dirs["extract"], "track.bin", within=[])


def test_sheet_tracks_passes_within_through(dirs):
    from utils.sheet_text import cue_tracks

    cue = dirs["extract"] / "game.cue"
    cue.write_text('FILE "track.bin" BINARY\n')

    assert sheet_tracks(cue, cue_tracks) is None                       # Volume default
    assert sheet_tracks(cue, cue_tracks, within=[dirs["extract"]]) == [
        dirs["extract"] / "track.bin",
    ]
