"""Library placement (#430): given a Platform, the folder its Images go in.

The inverse of ``Library.platform_folder``. A folder Library places by its
layout setting (RomM style ``roms/{platform}``, ES-DE/Batocera style
``{platform}``, or flat), naming Platform folders with the names detection
reads, so an Image placed for a Platform is detected back as that Platform. A
RomM Library places by RomM's own folder structure, and refuses when that
can't be told. A folder that isn't there yet is reported as one that will be
created; nothing here creates it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services import db as _db
from services import platform_detect
from services.rules.library import PlacementUnknown

from config import settings


@pytest.fixture(name="vol")
def _vol(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    platform_detect.clear_cache()
    return tmp_path


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path):
    from services import libraries
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    yield
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


def _library(vol: Path, layout: str | None = None, folder: str = "ROMs"):
    from services import libraries

    (vol / folder).mkdir(exist_ok=True)
    library = libraries.register(str(vol / folder))
    if layout is not None:
        libraries.update(library.id, layout=layout)
    return libraries.get(library.id)


def _detected(path: Path) -> str | None:
    # An extension no tool reads, so only the folder can name it.
    image = path / "Game.placed"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"x")
    return platform_detect.detect_uncached(str(image)).platform


# ---- each layout --------------------------------------------------------------


def test_romm_style_places_under_roms_by_romms_slug(vol, db):
    library = _library(vol, "romm")

    placed = library.placement("ps2")

    assert placed.folder == str(vol / "ROMs" / "roms" / "ps2")
    assert placed.platform == "ps2"
    assert placed.exists is False


def test_esde_style_places_in_the_root_by_es_des_system_name(vol, db):
    """ES-DE calls the GameCube folder ``gc``; RomM calls the Platform ``ngc``."""
    library = _library(vol, "esde")

    assert library.placement("ngc").folder == str(vol / "ROMs" / "gc")
    assert library.placement("ps2").folder == str(vol / "ROMs" / "ps2")


def test_flat_places_every_platform_in_the_root(vol, db):
    library = _library(vol, "flat")

    placed = library.placement("ps2")

    assert placed.folder == str(vol / "ROMs")
    assert placed.exists is True


def test_a_new_library_is_es_de_style(vol, db):
    from services import libraries

    library = _library(vol)

    assert libraries.describe(library)["layout"] == "esde"
    assert library.placement("psx").folder == str(vol / "ROMs" / "psx")


def test_an_existing_folder_is_reported_as_existing(vol, db):
    library = _library(vol, "romm")
    (vol / "ROMs" / "roms" / "ps2").mkdir(parents=True)

    placed = library.placement("ps2")

    assert placed.folder == str(vol / "ROMs" / "roms" / "ps2")
    assert placed.exists is True


def test_an_existing_folder_under_another_alias_is_used(vol, db):
    """A Platform folder the user already has, under any name detection reads
    for it, is where its Images go, spelled as listed."""
    library = _library(vol, "esde")
    (vol / "ROMs" / "GameCube").mkdir()

    placed = library.placement("ngc")

    assert placed.folder == str(vol / "ROMs" / "GameCube")
    assert placed.exists is True


def test_a_folder_mapped_to_the_platform_is_used(vol, db):
    from services import platform_mappings

    library = _library(vol, "esde")
    (vol / "ROMs" / "Handhelds").mkdir()
    platform_mappings.set_mapping(str(vol / "ROMs" / "Handhelds"), "psp")

    assert library.placement("psp").folder == str(vol / "ROMs" / "Handhelds")


def test_a_folder_named_for_the_platform_but_mapped_away_is_not_used(vol, db):
    """At one folder the mapping beats the name, for placing as for detecting."""
    from services import platform_mappings

    library = _library(vol, "esde")
    (vol / "ROMs" / "psp").mkdir()
    (vol / "ROMs" / "PlayStationPortable").mkdir()
    platform_mappings.set_mapping(str(vol / "ROMs" / "psp"), "ps2")
    platform_mappings.set_mapping(str(vol / "ROMs" / "PlayStationPortable"), "psp")

    assert library.placement("psp").folder == str(vol / "ROMs" / "PlayStationPortable")
    assert library.placement("ps2").folder == str(vol / "ROMs" / "psp")


def test_two_folders_for_one_platform_refuse(vol, db):
    library = _library(vol, "esde")
    (vol / "ROMs" / "ps2").mkdir()
    (vol / "ROMs" / "playstation2").mkdir()

    with pytest.raises(PlacementUnknown, match="more than one"):
        library.placement("ps2")


def test_a_file_where_the_folder_would_go_refuses(vol, db):
    library = _library(vol, "esde")
    (vol / "ROMs" / "ps2").write_bytes(b"not a folder")

    with pytest.raises(PlacementUnknown):
        library.placement("ps2")


def test_an_unknown_platform_refuses(vol, db):
    library = _library(vol, "esde")

    with pytest.raises(PlacementUnknown):
        library.placement("not-a-console")


def test_a_flat_library_whose_root_names_another_platform_refuses(vol, db):
    """An Image placed in a root named ``psx`` would be detected as PS1."""
    library = _library(vol, "flat", folder="psx")

    assert library.placement("psx").folder == str(vol / "psx")
    with pytest.raises(PlacementUnknown, match="psx"):
        library.placement("ps2")


# ---- placing and detecting agree ------------------------------------------------


@pytest.mark.parametrize("layout", ["romm", "esde"])
def test_every_platform_placed_is_detected_back(vol, db, layout):
    library = _library(vol, layout)

    for slug in sorted(platform_detect.known_slugs()):
        placed = library.placement(slug)
        assert _detected(Path(placed.folder)) == slug, (layout, slug, placed.folder)


@pytest.mark.parametrize("layout", ["romm", "esde"])
def test_placement_is_the_inverse_of_the_platform_folder(vol, db, layout):
    library = _library(vol, layout)

    placed = library.placement("ps2")
    image = Path(placed.folder) / "Game.iso"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"x")

    assert library.platform_folder(str(image)) == placed.folder


def test_a_flat_placement_round_trips_through_the_header(vol, db, monkeypatch):
    """Flat has no Platform folders: an Image placed there is detected by its
    header, and no folder evidence contradicts it."""
    library = _library(vol, "flat")
    placed = library.placement("ps2")
    image = Path(placed.folder) / "Game.iso"
    image.write_bytes(b"x")
    monkeypatch.setattr(
        platform_detect, "_header", lambda _path, _tools: ({"ps2"}, ["chdman"]),
    )

    detection = platform_detect.detect_uncached(str(image))

    assert detection.platform == "ps2" and detection.how == platform_detect.HEADER


# ---- the setting persists ---------------------------------------------------------


def test_the_layout_persists(vol, db):
    from services import libraries

    library = _library(vol, "romm")
    libraries.reset_for_tests()
    again = libraries.get(library.id)

    assert libraries.describe(again)["layout"] == "romm"
    assert again.placement("ps2").folder == str(vol / "ROMs" / "roms" / "ps2")


def test_an_unknown_layout_is_refused(vol, db):
    from services import libraries

    library = _library(vol)

    with pytest.raises(libraries.LibraryPathError) as refused:
        libraries.update(library.id, layout="sideways")
    assert refused.value.status == 422
    assert libraries.describe(libraries.get(library.id))["layout"] == "esde"


@pytest.mark.asyncio
async def test_the_library_api_sets_the_layout_and_lists_placements(vol, db):
    from routes import libraries as library_routes

    (vol / "ROMs" / "roms" / "ps2").mkdir(parents=True)
    (vol / "ROMs" / "roms" / "ps2" / "Game.iso").write_bytes(b"x")
    (vol / "ROMs" / "roms" / "gc").mkdir(parents=True)
    (vol / "ROMs" / "roms" / "gc" / "Game.rvz").write_bytes(b"x")
    created = await library_routes.register_library(
        library_routes.RegisterLibraryRequest(path=str(vol / "ROMs")),
    )

    updated = await library_routes.update_library(
        created["id"], library_routes.UpdateLibraryRequest(layout="romm"),
    )
    listed = await library_routes.library_placements(created["id"])

    assert updated["layout"] == "romm"
    assert listed["layout"] == "romm"
    by_platform = {p["platform"]: p for p in listed["placements"]}
    assert by_platform["ps2"]["folder"] == "roms/ps2"
    assert by_platform["ps2"]["exists"] is True
    assert by_platform["ngc"]["folder"] == "roms/gc"


@pytest.mark.asyncio
async def test_the_library_api_lists_a_folder_to_be_created(vol, db):
    """Switching layout leaves the files where they are; the listing says the
    new layout's folder will be created."""
    from routes import libraries as library_routes

    (vol / "ROMs" / "ps2").mkdir(parents=True)
    (vol / "ROMs" / "ps2" / "Game.iso").write_bytes(b"x")
    created = await library_routes.register_library(
        library_routes.RegisterLibraryRequest(path=str(vol / "ROMs")),
    )
    await library_routes.update_library(
        created["id"], library_routes.UpdateLibraryRequest(layout="romm"),
    )

    listed = await library_routes.library_placements(created["id"])

    assert listed["placements"] == [{
        "platform": "ps2", "name": "PlayStation 2",
        "folder": "roms/ps2", "exists": False, "error": None,
    }]


def test_the_api_refuses_an_unknown_layout():
    from pydantic import ValidationError
    from routes import libraries as library_routes

    with pytest.raises(ValidationError):
        library_routes.UpdateLibraryRequest(layout="sideways")


# ---- a RomM Library -------------------------------------------------------------


@pytest.fixture(name="romm")
def _romm(vol, monkeypatch):
    """RomM's library root at vol/romm, and a settable RomM platform list."""
    from services.romm import client as romm_client_module

    root = vol / "romm"
    root.mkdir()
    catalog: list[dict] = []
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str(root)),
    )
    monkeypatch.setattr(romm_client_module.romm_client, "platforms", lambda: list(catalog))
    return root, catalog


def test_romm_structure_a_places_under_roms_by_its_folder_name(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.append({"id": 4, "slug": "ngc", "fs_slug": "gc"})

    placed = romm_library.placement("ngc")

    assert placed.folder == str(root / "roms" / "gc")
    assert placed.exists is False
    assert _detected(Path(placed.folder)) == "ngc"


def test_romm_structure_b_places_in_the_platforms_roms_folder(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "ps2" / "roms").mkdir(parents=True)
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    placed = romm_library.placement("ps2")

    assert placed.folder == str(root / "ps2" / "roms")
    assert placed.exists is True
    assert _detected(Path(placed.folder)) == "ps2"


def test_romm_root_at_its_roms_folder_places_in_the_platform_folder(romm):
    """The library path set to RomM's ``roms`` folder itself, as
    ``RommClient.local_path`` maps it."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "ps2").mkdir()
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    assert romm_library.placement("ps2").folder == str(root / "ps2")


def test_a_platform_romm_has_no_record_of_yet_uses_its_slug(romm):
    from services.romm.library import romm_library

    root, _catalog = romm
    (root / "roms").mkdir()

    placed = romm_library.placement("psx")

    assert placed.folder == str(root / "roms" / "psx")
    assert placed.exists is False


def test_romm_structure_that_cant_be_told_refuses(romm):
    """No ``roms`` folder at the root and no folder for the Platform: either
    structure fits, so nothing is placed."""
    from services.romm.library import romm_library

    _root, catalog = romm
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    with pytest.raises(PlacementUnknown, match="folder structure"):
        romm_library.placement("ps2")


def test_a_romm_platform_folder_holding_only_bios_refuses(romm):
    """Structure B before any ROM: ``ps2/bios`` and no ``ps2/roms``."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "ps2" / "bios").mkdir(parents=True)
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    with pytest.raises(PlacementUnknown, match="folder structure"):
        romm_library.placement("ps2")


def test_two_romm_platforms_for_one_platform_refuse(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 3, "slug": "ps2", "fs_slug": "ps2"},
        {"id": 9, "slug": "playstation2", "fs_slug": "PS2 Games"},
    ])

    with pytest.raises(PlacementUnknown, match="more than one"):
        romm_library.placement("ps2")


def test_a_romm_folder_name_with_a_path_in_it_refuses(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "../ps2"})

    with pytest.raises(PlacementUnknown):
        romm_library.placement("ps2")


def test_a_romm_library_without_a_root_refuses(monkeypatch):
    from services.romm import client as romm_client_module
    from services.romm.library import romm_library

    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root", property(lambda _self: ""),
    )

    with pytest.raises(PlacementUnknown):
        romm_library.placement("ps2")


@pytest.mark.asyncio
async def test_the_library_api_lists_romms_placements(romm, db):
    from routes import libraries as library_routes

    root, catalog = romm
    (root / "roms" / "ps2").mkdir(parents=True)
    catalog.extend([
        {"id": 3, "slug": "ps2", "fs_slug": "ps2", "display_name": "PlayStation 2"},
        {"id": 4, "slug": "ngc", "fs_slug": "gc", "display_name": "GameCube"},
    ])

    listed = await library_routes.library_placements("romm")

    assert listed["layout"] is None
    assert listed["placements"] == [
        {"platform": "ngc", "name": "GameCube", "folder": "roms/gc",
         "exists": False, "error": None},
        {"platform": "ps2", "name": "PlayStation 2", "folder": "roms/ps2",
         "exists": True, "error": None},
    ]


# ---- the Libraries view -------------------------------------------------------------


def test_the_libraries_view_shows_the_layout_and_where_images_go():
    root = Path(__file__).resolve().parents[1]
    view = (root / "src/lib/components/views/RulesView.svelte").read_text()
    panel = (root / "src/lib/components/views/LibraryPlacement.svelte").read_text()
    endpoints = (root / "src/lib/api/endpoints.js").read_text()
    assert "<LibraryPlacement" in view
    assert "api.getLibraryPlacements" in panel
    assert "layout" in panel and "will be created" in panel
    assert "/placements`" in endpoints
    shots = (root / "shots.yml").read_text()
    readme = (root / "README.md").read_text()
    for theme in ("light", "dark"):
        assert f"output: docs/screenshots/rules-folder-layout-{theme}.png" in shots
        assert f"docs/screenshots/rules-folder-layout-{theme}.png" in readme
