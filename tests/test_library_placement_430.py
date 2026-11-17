"""Where a Library puts a Platform's Images (#430).

A folder Library's layout (ES-DE/Batocera, RomM or flat) decides the folder;
RomM's Library uses RomM's own structure. Placing and detecting agree: a file
placed in a Platform's folder is detected back as that Platform.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from services import platform_detect
from services.libraries.folder import FolderLibrary
from services.romm import library as romm_library_module

from .test_folder_library_303 import _db_fixture, _register  # noqa: F401


def _folder(root: Path, layout: str) -> FolderLibrary:
    return FolderLibrary(record_id=1, root=str(root), name="Test", layout=layout)


def _detected(folder: str) -> str | None:
    # An extension no tool reads, so only the folder can decide.
    return platform_detect.detect_uncached(os.path.join(folder, "Game.zzz"), mapped={}).platform


def test_every_esde_folder_is_an_alias_of_its_platform() -> None:
    for slug in platform_detect.known_slugs():
        assert platform_detect.canonical_slug(platform_detect.esde_folder(slug)) == slug


@pytest.mark.parametrize(("layout", "ps2", "ngc"), [
    ("esde", "ps2", "gc"),
    ("romm", "roms/ps2", "roms/ngc"),
    ("flat", "", ""),
])
def test_each_layout_places_platforms(tmp_path: Path, layout, ps2, ngc) -> None:
    folders = _folder(tmp_path, layout).placement_folders(["ps2", "ngc"])
    assert folders == {
        "ps2": os.path.normpath(tmp_path / ps2), "ngc": os.path.normpath(tmp_path / ngc),
    }


@pytest.mark.parametrize("layout", ["esde", "romm"])
def test_placed_images_are_detected_back(tmp_path: Path, layout) -> None:
    slugs = sorted(platform_detect.known_slugs())
    folders = _folder(tmp_path, layout).placement_folders(slugs)
    for slug in slugs:
        assert _detected(folders[slug]) == slug, (layout, slug, folders[slug])


def test_romm_layout_reads_platform_folders_under_roms(tmp_path: Path) -> None:
    library = _folder(tmp_path, "romm")
    (tmp_path / "roms" / "ps2").mkdir(parents=True)
    path = str(tmp_path / "roms" / "ps2" / "Ico.iso")
    assert library.platform_folder(path) == str(tmp_path / "roms" / "ps2")


@pytest.mark.asyncio
async def test_layout_persists_and_placements_report_missing_folders(db: Path) -> None:
    from routes import libraries as routes
    from services import libraries

    root = db / "games"
    root.mkdir()
    lib = await _register(root)
    assert lib["layout"] == "esde"
    saved = await routes.update_library(lib["id"], routes.UpdateLibraryRequest(layout="romm"))
    assert saved["layout"] == "romm"
    libraries.reset_for_tests()  # read back from the database
    assert libraries.get(lib["id"]).layout == "romm"

    (root / "roms" / "ps2").mkdir(parents=True)
    rows = {r["slug"]: r for r in (await routes.library_placements(lib["id"]))["placements"]}
    assert rows["ps2"]["folder"] == "roms/ps2" and rows["ps2"]["exists"] is True
    assert rows["ngc"]["folder"] == "roms/ngc" and rows["ngc"]["exists"] is False


def test_romm_uses_its_own_structure(tmp_path: Path, monkeypatch) -> None:
    client = romm_library_module.romm_client
    monkeypatch.setattr(type(client), "library_root", property(lambda _self: str(tmp_path)))
    # An operator's folder binding: RomM keeps GameCube in "gamecube".
    monkeypatch.setattr(client, "platforms", lambda: [
        {"id": 1, "slug": "ngc", "fs_slug": "gamecube"},
        {"id": 2, "slug": "ps2", "fs_slug": "ps2"},
    ])
    library = romm_library_module.romm_library
    # No roms/ folder: platforms sit in the root, as RommClient.local_path reads it.
    assert library.placement_folders(["ngc", "psx"]) == {
        "ngc": str(tmp_path / "gamecube"), "psx": str(tmp_path / "psx"),
    }
    (tmp_path / "roms").mkdir()
    folders = library.placement_folders(["ngc", "ps2", "psx"])
    assert folders == {
        "ngc": str(tmp_path / "roms" / "gamecube"),
        "ps2": str(tmp_path / "roms" / "ps2"),
        "psx": str(tmp_path / "roms" / "psx"),
    }
    for slug, folder in folders.items():
        assert _detected(folder) == slug
        os.mkdir(folder)
        assert library.platform_folder(os.path.join(folder, "Game.iso")) == folder
