"""Platform mapping and the Findings view (#304).

A mapping is the user saying "this folder is PSP". It is stored per Library,
keyed by the folder's path relative to the Library root, and is a source of
evidence inside Platform detection: header, then mapping, then folder name. A
mapping that disagrees with a header is a conflict, as a folder name is.

Findings are what the folder Library's walk noticed and a person has to decide
on: a Platform conflict, or an Unsorted Image. One open Finding per kind and
Image, upserted on every walk and cleared when the cause goes away. Dismissing
one hides it until its cause changes. Nothing here moves or deletes a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services import platform_detect

from .test_folder_library_303 import _db_fixture, _register, _tree  # noqa: F401
from .test_platform_detect import _blank, _gamecube, _ps2


def _map(path: Path, platform: str | None):
    from services import platform_mappings

    return platform_mappings.set_mapping(str(path), platform)


# ----------------------------------------------------------------------
# precedence: header > mapping > folder name
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mapping_names_the_platform_when_there_is_no_header(db: Path) -> None:
    misc = db / "roms" / "misc"
    image = _blank(misc / "Game.iso")
    await _register(db / "roms")
    _map(misc, "psp")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how, found.mapped) == ("psp", "mapping", "psp")


@pytest.mark.asyncio
async def test_a_mapping_outranks_the_folder_name(db: Path) -> None:
    image = _blank(db / "roms" / "ps2" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms" / "ps2", "psp")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how) == ("psp", "mapping")


@pytest.mark.asyncio
async def test_a_header_outranks_a_mapping_that_agrees(db: Path) -> None:
    image = _ps2(db / "roms" / "mine" / "Ico.iso")
    await _register(db / "roms")
    _map(db / "roms" / "mine", "ps2")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how) == ("ps2", "header")


@pytest.mark.asyncio
async def test_a_mapping_that_disagrees_with_the_header_is_a_conflict(db: Path) -> None:
    image = _gamecube(db / "roms" / "mine" / "Melee.iso")
    await _register(db / "roms")
    _map(db / "roms" / "mine", "psp")

    found = platform_detect.detect_platform(image)

    assert found.platform is None
    assert found.how == "conflict"
    assert (found.header, found.mapped) == ("ngc", "psp")
    assert "psp" in found.reason and "ngc" in found.reason


@pytest.mark.asyncio
async def test_the_nearest_mapped_folder_answers(db: Path) -> None:
    image = _blank(db / "roms" / "a" / "b" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms" / "a", "psx")
    _map(db / "roms" / "a" / "b", "psp")

    assert platform_detect.detect_platform(image).platform == "psp"


@pytest.mark.asyncio
async def test_changing_a_mapping_re_detects_a_cached_image(db: Path) -> None:
    misc = db / "roms" / "misc"
    image = _blank(misc / "Game.iso")
    await _register(db / "roms")
    assert platform_detect.detect_platform(image).how == "unsorted"

    _map(misc, "psp")
    assert platform_detect.detect_platform(image).platform == "psp"

    _map(misc, "ps2")
    assert platform_detect.detect_platform(image).platform == "ps2"

    _map(misc, None)
    assert platform_detect.detect_platform(image).how == "unsorted"


@pytest.mark.asyncio
async def test_a_folder_library_sees_a_new_mapping_without_waiting(db: Path) -> None:
    from services import libraries

    misc = db / "roms" / "misc"
    _blank(misc / "Game.iso")
    await _register(db / "roms")
    library = libraries.folder_libraries()[0]
    assert library.summary()["unsorted"] == 1

    _map(misc, "psp")

    summary = library.summary()
    assert summary["unsorted"] == 0
    assert [p["id"] for p in summary["platforms"]] == ["psp"]


@pytest.mark.asyncio
async def test_a_mapping_is_stored_relative_to_the_library_root(db: Path) -> None:
    from services import db as _db
    from services import libraries

    (db / "roms" / "misc").mkdir(parents=True)
    await _register(db / "roms")
    _map(db / "roms" / "misc", "psp")

    with _db.SessionLocal() as session:
        rows = session.query(_db.PlatformMappingRecord).all()
    library = libraries.folder_libraries()[0]
    assert [(r.library_id, r.folder, r.platform) for r in rows] == [
        (library.id, "misc", "psp"),
    ]


def test_a_mapping_outside_every_library_is_refused(db: Path) -> None:
    from services import platform_mappings

    loose = db / "loose"
    loose.mkdir()
    with pytest.raises(platform_mappings.MappingError) as err:
        _map(loose, "psp")
    assert err.value.status == 404


@pytest.mark.asyncio
async def test_an_unknown_platform_is_refused(db: Path) -> None:
    from services import platform_mappings

    (db / "roms" / "misc").mkdir(parents=True)
    await _register(db / "roms")
    with pytest.raises(platform_mappings.MappingError) as err:
        _map(db / "roms" / "misc", "not-a-console")
    assert err.value.status == 422
