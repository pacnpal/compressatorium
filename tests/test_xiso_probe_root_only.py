"""The Xbox console probe reads only the root directory table.

A trimmed XISO has no layout that names its console, so the probe looks for the
boot executable in the root. Walking every child table would spend the probe's
read budget on an image with many folders and leave it Unsorted.
"""
from __future__ import annotations

import struct

from .test_xiso_service import MAGIC, SECTOR, _dir_table

_DIRS = 2100   # child tables alone would be ~4.1 MiB, over the 4 MiB probe budget


def _trimmed_with_many_dirs(path) -> str:
    root_sector = 33
    first_child = 2000
    entries = [("default.xbe", 100, 3000, 0x20)]   # past the ~21-sector root table
    entries += [(f"d{i:04d}", first_child + i, SECTOR, 0x10) for i in range(_DIRS)]
    root = _dir_table(entries)
    with open(path, "wb") as fh:
        fh.seek(0x10000)
        fh.write(MAGIC + struct.pack("<II", root_sector, len(root)) + b"\0" * 0x7D0 + MAGIC)
        fh.seek(root_sector * SECTOR)
        fh.write(root)
        fh.seek(100 * SECTOR)
        fh.write(b"\xab" * 3000)
        for i in range(_DIRS):          # unused-slot tables: valid, and empty
            fh.seek((first_child + i) * SECTOR)
            fh.write(b"\xff" * SECTOR)
    return str(path)


def test_console_comes_from_the_root_however_many_folders(tmp_path):
    from services.extract_xiso import detect_console

    path = _trimmed_with_many_dirs(tmp_path / "game.iso")

    assert detect_console(path) == "Xbox"


def test_detection_names_the_trimmed_image(tmp_path, monkeypatch):
    from config import settings
    from services import platform_detect

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    platform_detect.clear_cache()
    (tmp_path / "pile").mkdir()
    path = _trimmed_with_many_dirs(tmp_path / "pile" / "game.iso")

    assert platform_detect.detect_platform(path).platform == "xbox"
    platform_detect.clear_cache()


def test_the_full_walk_still_counts_every_folder(tmp_path):
    from services.extract_xiso import walk_image

    path = _trimmed_with_many_dirs(tmp_path / "game.iso")

    assert walk_image(path)["dir_count"] == _DIRS
