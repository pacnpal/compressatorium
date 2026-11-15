"""Grouping files into Images (#303 fix round 1).

A sheet (``.cue``, ``.gdi``) owns the track files it references, so those are
never Images of their own. A tool's ``source_companions`` (jwud's split parts)
are owned by their primary the same way. Playlists (``.m3u``) are never
Images. Everything else stays: a ``.bin`` no sheet references is listed on its
own.
"""

from __future__ import annotations

from pathlib import Path

from services.image_sets import group_images, image_members


def _write(path: Path, data: bytes | str = b"\0" * 16) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)
    return str(path)


def _psx_tree(root: Path) -> dict[str, str]:
    return {
        "single_cue": _write(
            root / "Single" / "Game.cue",
            'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
        ),
        "single_bin": _write(root / "Single" / "Game.bin"),
        "multi_cue": _write(
            root / "Multi" / "Multi.cue",
            'FILE "Multi (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n'
            'FILE "Multi (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n',
        ),
        "track1": _write(root / "Multi" / "Multi (Track 1).bin"),
        "track2": _write(root / "Multi" / "Multi (Track 2).bin"),
        "loose": _write(root / "Loose.bin"),
        "playlist": _write(root / "Multi.m3u", "Multi/Multi.cue\n"),
    }


def test_a_sheet_owns_its_tracks(tmp_path: Path) -> None:
    files = _psx_tree(tmp_path / "psx")

    images = group_images(files.values())

    assert sorted(Path(p).name for p in images) == ["Game.cue", "Loose.bin", "Multi.cue"]


def test_image_members_lists_the_tracks_of_a_sheet(tmp_path: Path) -> None:
    files = _psx_tree(tmp_path / "psx")

    members = image_members(files["multi_cue"])

    assert sorted(Path(p).name for p in members) == ["Multi (Track 1).bin", "Multi (Track 2).bin"]
    assert image_members(files["loose"]) == []


def test_a_gdi_owns_its_tracks(tmp_path: Path) -> None:
    gdi = _write(
        tmp_path / "dc" / "Game.gdi",
        "3\n1 0 4 2352 track01.bin 0\n2 450 0 2352 track02.raw 0\n"
        "3 45000 4 2352 track03.bin 0\n",
    )
    tracks = [
        _write(tmp_path / "dc" / name) for name in ("track01.bin", "track02.raw", "track03.bin")
    ]

    assert group_images([gdi, *tracks]) == [gdi]


def test_a_reference_outside_the_sheets_folder_owns_nothing(tmp_path: Path) -> None:
    outside = _write(tmp_path / "Other.bin")
    sheet = _write(tmp_path / "sub" / "Evil.cue", 'FILE "../Other.bin" BINARY\n')

    assert sorted(group_images([sheet, outside])) == sorted([sheet, outside])


def test_split_parts_belong_to_their_first_part(tmp_path: Path) -> None:
    parts = [_write(tmp_path / "wiiu" / f"game_part{i}.wud") for i in (1, 2, 3)]

    assert group_images(parts) == [parts[0]]
