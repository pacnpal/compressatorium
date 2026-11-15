"""Deleting an archive Source never takes another Image with it (#306)."""
from __future__ import annotations

import zipfile

from utils.delete_plan import build_delete_plan


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name in members:
            zf.writestr(name, b"x" * 64)
    return path


def test_an_archive_with_another_image_is_not_deleted(tmp_path):
    pack = _zip(tmp_path / "pack.zip", ["Game.iso", "Other.nsp"])

    plan = build_delete_plan(f"{pack}::Game.iso")

    assert any("Other.nsp" in error for error in plan["errors"])


def test_a_track_set_or_notes_beside_the_image_are_not_other_images(tmp_path):
    cue = _zip(tmp_path / "cd.zip", ["Game.cue", "Game.bin", "readme.txt"])
    single = _zip(tmp_path / "one.zip", ["Game.iso", "__MACOSX/._Game.iso"])

    for source in (f"{cue}::Game.cue", f"{single}::Game.iso"):
        assert build_delete_plan(source)["errors"] == [], source
