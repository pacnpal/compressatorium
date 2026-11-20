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
        "id": "ps2", "platform": "ps2", "name": "PlayStation 2",
        "folder": "roms/ps2", "exists": False, "error": None, "rom_count": None,
        "guessed": False, "why": None, "new_images_here": None, "choices": [], "picked": None,
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


def test_romm_structure_that_cant_be_told_guesses_romms_default(romm):
    """#512: no ``roms`` folder at the root and no folder for the Platform:
    RomM's default structure, ``<root>/roms/<folder>``, marked as a guess."""
    from services.romm.library import romm_library

    root, catalog = romm
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    placed = romm_library.placement("ps2")

    assert placed.folder == str(root / "roms" / "ps2")
    assert placed.exists is False
    assert placed.guessed is True
    assert "default" in placed.why


def test_a_romm_platform_folder_holding_only_bios_guesses_its_roms_folder(romm):
    """Structure B before any ROM: ``ps2/bios`` and no ``ps2/roms``, so
    ``ps2/roms``, marked as a guess."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "ps2" / "bios").mkdir(parents=True)
    catalog.append({"id": 3, "slug": "ps2", "fs_slug": "ps2"})

    placed = romm_library.placement("ps2")

    assert placed.folder == str(root / "ps2" / "roms")
    assert placed.guessed is True


def test_two_romm_platforms_for_one_platform_place_in_the_one_with_roms(romm):
    """#512: the record holding ROMs is the one, whatever is on disk."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 3, "slug": "ps2", "fs_slug": "ps2", "rom_count": 0},
        {"id": 9, "slug": "playstation2", "fs_slug": "PS2 Games", "rom_count": 12,
         "display_name": "PlayStation 2"},
    ])

    placed = romm_library.placement("ps2")

    assert placed.folder == str(root / "roms" / "PS2 Games")
    assert placed.guessed is False
    assert placed.why == "RomM's PlayStation 2 folder, 12 ROMs: the only one with ROMs"
    assert placed.choices == (("PS2 Games", 12), ("ps2", 0))


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
        {"id": "4", "platform": "ngc", "name": "GameCube", "folder": "roms/gc",
         "exists": False, "error": None, "rom_count": None, "guessed": False,
         "why": None, "new_images_here": None, "choices": [], "picked": None},
        {"id": "3", "platform": "ps2", "name": "PlayStation 2", "folder": "roms/ps2",
         "exists": True, "error": None, "rom_count": None, "guessed": False,
         "why": None, "new_images_here": None, "choices": [], "picked": None},
    ]


# ---- review round (Codex on PR #437) -------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_reads_romms_catalog_once(romm, db, monkeypatch):
    """Codex PRRT_kwDOQ9nULM64es7O: one catalog per listing, not one per
    Platform on top of the listing's own."""
    from routes import libraries as library_routes
    from services.romm import client as romm_client_module

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend(
        {"id": n, "slug": slug, "fs_slug": slug, "display_name": slug}
        for n, slug in enumerate(("ps2", "psx", "ngc", "dc", "saturn"), start=1)
    )
    calls = []
    monkeypatch.setattr(
        romm_client_module.romm_client, "platforms",
        lambda: calls.append(1) or list(catalog),
    )

    listed = await library_routes.library_placements("romm")

    assert len(listed["placements"]) == 5
    assert all(row["error"] is None for row in listed["placements"])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_two_romm_platforms_for_one_slug_are_two_rows_with_their_own_ids(romm, db):
    """Codex PRRT_kwDOQ9nULM64es7J: both rows stay, each in its own folder
    (#512), and each has its own key (the RomM platform id), so the view can list both."""
    from routes import libraries as library_routes

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 3, "slug": "ps2", "fs_slug": "ps2", "display_name": "PlayStation 2"},
        {"id": 9, "slug": "playstation2", "fs_slug": "PS2 Games", "display_name": "PS2 (old)"},
    ])

    rows = (await library_routes.library_placements("romm"))["placements"]

    assert sorted(row["id"] for row in rows) == ["3", "9"]
    assert {row["platform"] for row in rows} == {"ps2"}
    assert all(row["error"] is None for row in rows)
    assert sorted(row["folder"] for row in rows) == ["roms/PS2 Games", "roms/ps2"]


def test_an_existing_image_keeps_its_platform_folder_after_a_layout_change(vol, db):
    """Codex PRRT_kwDOQ9nULM64es7B: the layout decides where new Images go,
    not which folder holds the ones already there. Switched from ES-DE to
    RomM style (no ``roms`` folder yet), an Image in ``<root>/ps2`` is still
    in the ps2 Platform folder."""
    from services import libraries

    library = _library(vol, "esde")
    image = vol / "ROMs" / "ps2" / "Game.iso"
    image.parent.mkdir()
    image.write_bytes(b"x")
    libraries.update(library.id, layout="romm")
    library = libraries.get(library.id)

    assert library.platform_folder(str(image)) == str(vol / "ROMs" / "ps2")


def test_a_romm_style_image_in_an_es_de_style_library_has_its_platform_folder(vol, db):
    library = _library(vol, "esde")
    image = vol / "ROMs" / "roms" / "ps2" / "Game.iso"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"x")

    assert library.platform_folder(str(image)) == str(vol / "ROMs" / "roms" / "ps2")


@pytest.mark.asyncio
async def test_the_gamelist_follows_an_image_converted_after_a_layout_change(vol, db):
    """The same, through the gamelist Referrer: the Source isn't kept."""
    from services import libraries, referrers

    library = _library(vol, "esde")
    libraries.update(
        library.id, es_frontend=True,
        gamelists_folder=libraries.check_gamelists_folder(str(_mkdir(vol / "gamelists"))),
    )
    source = vol / "ROMs" / "ps2" / "Game.iso"
    source.parent.mkdir()
    source.write_bytes(b"disc")
    output = source.with_suffix(".chd")
    output.write_bytes(b"output")
    gamelist = _mkdir(vol / "gamelists" / "ps2") / "gamelist.xml"
    gamelist.write_bytes(b"<gameList><game><path>./Game.iso</path></game></gameList>")
    libraries.update(library.id, layout="romm")

    outcome = await referrers.update_before_source_removal(
        str(source), {str(source): str(output)}, library=libraries.get(library.id),
    )

    assert outcome.ok, outcome.reason
    assert b"./Game.chd" in gamelist.read_bytes()


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


_LATEST = Path(__file__).resolve().parents[1] / "src/lib/util/latestOnly.js"


def test_an_older_placements_answer_never_overwrites_a_newer_one(tmp_path):
    """Codex PRRT_kwDOQ9nULM64es7U: two loads for one Library (a layout
    change while the first is out); the first answers last and is dropped,
    for its data and for its error alike."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    assert _LATEST.is_file(), "src/lib/util/latestOnly.js is missing"
    script = tmp_path / "latest.mjs"
    script.write_text(
        f"import {{ LatestOnly }} from {json.dumps(_LATEST.as_uri())};\n"
        "const deferred = () => { let ok, no; const p = new Promise((a, b) => { ok = a; no = b; });"
        " return { p, ok, no }; };\n"
        "const latest = new LatestOnly();\n"
        "const older = deferred(), newer = deferred();\n"
        "const first = latest.run(() => older.p);\n"
        "const second = latest.run(() => newer.p);\n"
        "newer.ok('romm');\n"
        "const b = await second;\n"
        "older.ok('esde');\n"
        "const a = await first;\n"
        "const failing = deferred(), winning = deferred();\n"
        "const third = latest.run(() => failing.p);\n"
        "const fourth = latest.run(() => winning.p);\n"
        "winning.ok('flat');\n"
        "failing.no(new Error('late'));\n"
        "const c = await third, d = await fourth;\n"
        "process.stdout.write(JSON.stringify({ a, b, c: { current: c.current },"
        " d: { current: d.current, value: d.value } }));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "a": {"current": False, "value": "esde"},
        "b": {"current": True, "value": "romm"},
        "c": {"current": False},
        "d": {"current": True, "value": "flat"},
    }


def test_the_panel_keys_rows_by_id_and_drops_superseded_answers():
    panel = (
        Path(__file__).resolve().parents[1] / "src/lib/components/views/LibraryPlacement.svelte"
    ).read_text()
    loader = (
        Path(__file__).resolve().parents[1] / "src/lib/util/libraryPlacements.js"
    ).read_text()
    assert "{#each shown.placements as row (row.id)}" in panel
    assert "new PlacementsLoad(" in panel
    assert "new LatestOnly()" in loader
    assert "if (!result.current) return null;" in loader


# ---- the Libraries view -------------------------------------------------------------


def test_the_libraries_view_shows_the_layout_and_where_images_go():
    root = Path(__file__).resolve().parents[1]
    view = (root / "src/lib/components/library/LibraryAutomation.svelte").read_text()
    panel = (root / "src/lib/components/views/LibraryPlacement.svelte").read_text()
    endpoints = (root / "src/lib/api/endpoints.js").read_text()
    assert "<LibraryPlacement" in view
    assert "api.getLibraryPlacements" in panel
    assert "layout" in panel and "will be created" in panel
    assert "/placements`" in endpoints
    shots = (root / "shots.yml").read_text()
    readme = (root / "README.md").read_text()
    for theme in ("light", "dark"):
        assert f"output: docs/screenshots/library-placement-{theme}.png" in shots
        assert f"docs/screenshots/library-placement-{theme}.png" in readme


def test_the_panel_waits_for_the_views_own_listing():
    """A folder Library is walked one listing at a time: a second walk while
    one runs fails at once (``listing_in_progress``). The panel's placements
    list the Library's Platforms too, so it must not start while the view's
    own Platform listing is running, or one of the two fails (the Rules view
    then showed no Platform rows; the screenshots check caught it)."""
    root = Path(__file__).resolve().parents[1]
    view = (root / "src/lib/components/library/LibraryAutomation.svelte").read_text()
    panel = (root / "src/lib/components/views/LibraryPlacement.svelte").read_text()
    assert "listing={source.platformsLoading" in view
    assert "if (!untrack(() => listing)) load(id)" in panel
    assert "listing_in_progress" in panel


# ---- #512: RomM's second, empty record for a Platform; junk folders ----------------


def test_two_romm_platforms_place_in_the_one_with_roms_not_the_one_on_disk(romm):
    """#512: RomM keeps an empty record beside the real one (``famicom``
    beside ``nes``). The one with ROMs is it, even if only the other's folder
    is there."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms" / "famicom").mkdir(parents=True)
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 4933, "display_name": "NES"},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 0},
    ])

    placed = romm_library.placement("nes")

    assert placed.folder == str(root / "roms" / "nes")
    assert placed.guessed is False
    assert placed.why == "RomM's NES folder, 4933 ROMs: the only one with ROMs"


def test_two_romm_platforms_naming_one_folder_place_there(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 14, "slug": "gamegear", "fs_slug": "gamegear", "rom_count": 300},
        {"id": 43, "slug": "gg", "fs_slug": "gamegear", "rom_count": 2},
    ])

    placed = romm_library.placement("gamegear")

    assert placed.folder == str(root / "roms" / "gamegear")
    assert placed.guessed is False
    assert placed.choices == ()


def test_two_romm_platforms_both_with_roms_guess_the_one_with_most(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 40, "display_name": "NES"},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 900,
         "display_name": "Famicom"},
    ])

    placed = romm_library.placement("nes")

    assert placed.folder == str(root / "roms" / "famicom")
    assert placed.guessed is True
    assert placed.why == "RomM's Famicom folder, 900 ROMs: it has the most ROMs"
    assert placed.choices == (("famicom", 900), ("nes", 40))


def test_two_empty_romm_platforms_guess_the_one_named_as_the_platform(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 0},
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 0},
    ])

    placed = romm_library.placement("nes")

    assert placed.folder == str(root / "roms" / "nes")
    assert placed.guessed is True


@pytest.mark.asyncio
async def test_a_saved_pick_wins_over_the_guess(romm, db):
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 4933, "display_name": "NES"},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 1,
         "display_name": "Famicom"},
    ])
    await romm_settings.load(force=True)

    await romm_library.pick_folder("nes", "famicom")
    placed = romm_library.placement("nes")

    assert placed.folder == str(root / "roms" / "famicom")
    assert placed.guessed is False
    assert placed.why == "RomM's Famicom folder, 1 ROM: your choice"
    # Kept across a restart: it's in RomM's settings row.
    romm_settings.reset_for_tests()
    await romm_settings.load(force=True)
    assert romm_library.placement("nes").folder == str(root / "roms" / "famicom")

    await romm_library.pick_folder("nes", None)
    assert romm_library.placement("nes").folder == str(root / "roms" / "nes")


@pytest.mark.asyncio
async def test_a_pick_must_be_one_of_romms_folders_for_the_platform(romm, db):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 3},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 1},
    ])

    with pytest.raises(PlacementUnknown, match="isn't one of"):
        await romm_library.pick_folder("nes", "elsewhere")
    with pytest.raises(PlacementUnknown, match="isn't one folder"):
        await romm_library.pick_folder("nes", "../elsewhere")


def test_a_bad_folder_name_beside_a_good_one_is_never_picked(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 3},
        {"id": 44, "slug": "famicom", "fs_slug": "../famicom", "rom_count": 900},
    ])

    assert romm_library.placement("nes").folder == str(root / "roms" / "nes")


@pytest.mark.asyncio
async def test_the_listing_places_each_record_in_its_own_folder(romm, db):
    """#512: two Game Gear records show their own two folders, and the one
    new Images go to says so, with why and the other choices."""
    from routes import libraries as library_routes

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 14, "slug": "gamegear", "fs_slug": "gamegear", "rom_count": 412,
         "display_name": "Sega Game Gear"},
        {"id": 43, "slug": "gg", "fs_slug": "gg", "rom_count": 0,
         "display_name": "Sega Game Gear"},
    ])

    rows = {
        row["id"]: row
        for row in (await library_routes.library_placements("romm"))["placements"]
    }

    assert rows["14"]["folder"] == "roms/gamegear" and rows["14"]["error"] is None
    assert rows["43"]["folder"] == "roms/gg" and rows["43"]["error"] is None
    assert rows["14"]["new_images_here"] is True
    assert rows["43"]["new_images_here"] is False
    assert rows["14"]["rom_count"] == 412
    assert rows["14"]["choices"] == [
        {"name": "gamegear", "rom_count": 412}, {"name": "gg", "rom_count": 0},
    ]
    assert rows["43"]["choices"] == []
    assert rows["14"]["picked"] == "gamegear" and rows["43"]["picked"] is None
    assert rows["14"]["why"] == "RomM's Sega Game Gear folder, 412 ROMs: the only one with ROMs"


@pytest.mark.asyncio
async def test_the_pick_api_saves_the_folder_and_refuses_a_stranger(romm, db):
    from fastapi import HTTPException
    from routes import libraries as library_routes
    from services.romm import settings as romm_settings

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 5},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 9},
    ])
    await romm_settings.load(force=True)

    await library_routes.pick_placement(
        "romm", "nes", library_routes.PlacementPick(folder="nes"),
    )
    rows = {
        row["id"]: row
        for row in (await library_routes.library_placements("romm"))["placements"]
    }
    assert rows["8"]["new_images_here"] is True
    assert rows["8"]["guessed"] is False
    assert rows["8"]["why"].endswith("your choice")

    with pytest.raises(HTTPException) as refused:
        await library_routes.pick_placement(
            "romm", "nes", library_routes.PlacementPick(folder="snes"),
        )
    assert refused.value.status_code == 422


def test_the_panel_offers_the_other_folders_on_a_platform_with_several():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "src/lib/components/views/LibraryPlacement.svelte").read_text()
    endpoints = (root / "src/lib/api/endpoints.js").read_text()
    assert "row.choices.length > 1" in panel
    assert "api.pickLibraryPlacement" in panel
    assert "row.why" in panel
    assert "pickLibraryPlacement:" in endpoints


def test_a_romm_platform_for_a_junk_folder_is_not_listed(romm):
    """#512: RomM scans Synology's ``@eaDir`` as a platform; it is never one."""
    from services.romm.library import romm_library

    _root, catalog = romm
    catalog.extend([
        {"id": 3, "slug": "ps2", "fs_slug": "ps2"},
        {"id": 50, "slug": "@eadir", "fs_slug": "@eaDir", "display_name": "@Eadir"},
    ])

    assert [p.id for p in romm_library.platforms()] == ["3"]


def test_a_new_platform_beside_flat_platform_folders_keeps_the_flat_layout(romm):
    """Codex PR #516: the root is RomM's ``roms`` folder itself (``nes``
    directly in it). A Platform with no folder yet goes beside it, never into
    a new ``roms`` folder, which would make ``RommClient.local_path`` stop
    stripping ``roms/`` and lose every existing record."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "nes").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 3},
        {"id": 9, "slug": "snes", "fs_slug": "snes", "rom_count": 0},
    ])

    placed = romm_library.placement("snes")

    assert placed.folder == str(root / "snes")
    assert not (root / "roms").exists()


def test_a_new_platform_beside_per_platform_roms_folders_keeps_that_layout(romm):
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "nes" / "roms").mkdir(parents=True)
    catalog.append({"id": 9, "slug": "snes", "fs_slug": "snes"})

    assert romm_library.placement("snes").folder == str(root / "snes" / "roms")


@pytest.mark.asyncio
async def test_a_picked_folder_that_cant_take_images_keeps_the_chooser(romm, db):
    """Codex PR #516: the picked folder is a file. Its row says why and still
    offers the other folders, so the pick can be undone from the UI."""
    from routes import libraries as library_routes
    from services.romm import settings as romm_settings
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms" / "nes").mkdir(parents=True)
    (root / "roms" / "famicom").write_text("not a folder")
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 5},
        {"id": 44, "slug": "famicom", "fs_slug": "famicom", "rom_count": 1},
    ])
    await romm_settings.load(force=True)
    await romm_library.pick_folder("nes", "famicom")

    rows = {
        row["id"]: row
        for row in (await library_routes.library_placements("romm"))["placements"]
    }

    assert rows["44"]["error"]
    assert rows["44"]["picked"] == "famicom"
    assert [c["name"] for c in rows["44"]["choices"]] == ["famicom", "nes"]
    assert rows["8"]["new_images_here"] is False


@pytest.mark.asyncio
async def test_a_pick_of_a_folder_name_with_a_path_in_it_is_refused(romm, db):
    """CodeRabbit PR #516: the only record's folder is ``../nes``; saving it
    would leave a pick every later placement refuses."""
    from services.romm.library import romm_library

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.append({"id": 8, "slug": "nes", "fs_slug": "../nes", "rom_count": 3})

    with pytest.raises(PlacementUnknown, match="isn't one folder"):
        await romm_library.pick_folder("nes", "../nes")


@pytest.mark.asyncio
async def test_an_automatic_pick_detection_refuses_keeps_the_chooser(romm, db):
    """CodeRabbit PR #516: the folder with most ROMs reads as another
    Platform; its row still offers the valid one."""
    from routes import libraries as library_routes

    root, catalog = romm
    (root / "roms").mkdir()
    catalog.extend([
        {"id": 8, "slug": "nes", "fs_slug": "nes", "rom_count": 3},
        {"id": 9, "slug": "nes", "fs_slug": "ps2", "rom_count": 900},
    ])

    rows = {
        row["id"]: row
        for row in (await library_routes.library_placements("romm"))["placements"]
    }

    assert rows["9"]["error"] and rows["9"]["picked"] == "ps2"
    assert [c["name"] for c in rows["9"]["choices"]] == ["nes", "ps2"]


def test_the_panel_can_go_back_to_the_automatic_pick_and_resets_a_failed_one():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "src/lib/components/views/LibraryPlacement.svelte").read_text()
    assert "Automatic" in panel and "folder || null" in panel
    assert "{#key pickReset}" in panel and "pickReset += 1" in panel
