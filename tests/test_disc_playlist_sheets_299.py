"""Create .m3u reads track sheets through utils.sheet_text (#294), not a parser of its own.

Importing `services.disc_playlist` must work next to #294 (it used to import the
delete plan's old sheet parsers, which #294 removed), and the ticked track files
of a `.cue` / `.gdi` collapse into their sheet by the shared reader's rules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.routes import files as files_routes
from app.services import disc_playlist


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(files_routes.settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(files_routes.settings, "data_mount_root", str(tmp_path))
    return tmp_path


def _cue_disc(folder: Path, name: str) -> list[str]:
    (folder / f"{name}.bin").write_bytes(b"x")
    (folder / f"{name}.cue").write_text(
        f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
    )
    return [str(folder / f"{name}.cue"), str(folder / f"{name}.bin")]


def _gdi_disc(folder: Path, name: str) -> list[str]:
    (folder / "track01.bin").write_bytes(b"x")
    (folder / "track03.bin").write_bytes(b"x")
    (folder / f"{name}.gdi").write_text(
        '2\n1 0 4 2352 "track01.bin" 0\n3 45000 4 2352 "track03.bin" 0\n',
    )
    return [str(folder / f"{name}.gdi"), str(folder / "track01.bin"), str(folder / "track03.bin")]


def test_a_cue_disc_set_collapses_its_ticked_tracks(vol: Path):
    paths = _cue_disc(vol, "Game (Disc 1)") + _cue_disc(vol, "Game (Disc 2)")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).cue"]
    assert any("2 track files" in w for w in plan.warnings)


def test_a_gdi_disc_set_collapses_its_ticked_tracks(vol: Path):
    paths = _gdi_disc(vol, "Game (Disc 1)")
    # a second, cue-based disc in the same folder
    paths += _cue_disc(vol, "Game (Disc 2)")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).gdi", "Game (Disc 2).cue"]
    assert any("3 track files" in w for w in plan.warnings)


def test_a_sheet_the_shared_reader_refuses_names_no_tracks(vol: Path):
    # A NUL byte makes a sheet unreadable (chdman would end a token early), so
    # its ticked .bin is not known to be a track and the selection is refused.
    paths = _cue_disc(vol, "Game (Disc 1)")
    (vol / "Game (Disc 1).cue").write_bytes(b'FILE "Game (Disc 1).bin"\0 BINARY\n')
    paths += _cue_disc(vol, "Game (Disc 2)")[:1]
    with pytest.raises(disc_playlist.PlaylistError, match="not a disc image"):
        disc_playlist.plan_playlist(paths)


def _cue_naming(folder: Path, sheet: str, track: str) -> list[str]:
    (folder / track).write_bytes(b"x")
    (folder / sheet).write_text(
        f'FILE "{track}" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n',
    )
    return [str(folder / sheet), str(folder / track)]


def test_iso_tracks_named_by_selected_sheets_are_not_discs_of_their_own(vol: Path):
    paths = _cue_naming(vol, "Game (Disc 1).cue", "track1.iso")
    paths += _cue_naming(vol, "Game (Disc 2).cue", "track2.iso")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).cue"]
    assert any("2 track files" in w for w in plan.warnings)


def test_iso_tracks_with_the_sheets_stems_are_not_duplicate_discs(vol: Path):
    paths = _cue_naming(vol, "Game (Disc 1).cue", "Game (Disc 1).iso")
    paths += _cue_naming(vol, "Game (Disc 2).cue", "Game (Disc 2).iso")
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).cue"]


def test_an_iso_no_selected_sheet_names_is_still_a_disc(vol: Path):
    paths = _cue_naming(vol, "Game (Disc 1).cue", "track1.iso")
    (vol / "Game (Disc 2).iso").write_bytes(b"x")
    paths.append(str(vol / "Game (Disc 2).iso"))
    plan = disc_playlist.plan_playlist(paths)
    assert plan.entries == ["Game (Disc 1).cue", "Game (Disc 2).iso"]
