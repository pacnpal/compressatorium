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


# ----------------------------------------------------------------------
# the Findings store
# ----------------------------------------------------------------------


def _conflict(key: str, header: str = "ngc", folder: str = "ps2"):
    from services import findings

    return findings.Observed(
        kind=findings.PLATFORM_CONFLICT, image_key=key, path=f"/vol/roms/{key}",
        reason=f"in the {folder} folder, but the header says {header}",
        cause={"header": header, "folder": folder, "mapped": None},
    )


def _open(**kwargs) -> list[dict]:
    from services import findings

    return findings.list_findings(**kwargs)


def test_a_finding_is_stored_once_per_kind_and_image(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")], now="2026-11-01T00:00:00Z")
    findings.reconcile("folder:1", [_conflict("ps2/a.iso")], now="2026-11-02T00:00:00Z")

    rows = _open()
    assert len(rows) == 1
    row = rows[0]
    assert (row["library_id"], row["image_key"], row["kind"]) == (
        "folder:1", "ps2/a.iso", "platform_conflict",
    )
    assert row["first_seen"] == "2026-11-01T00:00:00Z"
    assert row["last_seen"] == "2026-11-02T00:00:00Z"
    assert row["reason"] == "in the ps2 folder, but the header says ngc"


def test_a_finding_clears_when_its_cause_goes_away(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso"), _conflict("ps2/b.iso")])
    findings.reconcile("folder:1", [_conflict("ps2/b.iso")])

    assert [r["image_key"] for r in _open()] == ["ps2/b.iso"]


def test_a_walk_only_clears_its_own_library(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    findings.reconcile("folder:2", [])

    assert [r["library_id"] for r in _open()] == ["folder:1"]


def test_dismissing_hides_a_finding_while_its_cause_is_the_same(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    finding_id = _open()[0]["id"]

    assert findings.dismiss(finding_id) is True
    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])

    assert _open() == []
    dismissed = _open(include_dismissed=True)
    assert [(r["id"], r["dismissed"]) for r in dismissed] == [(finding_id, True)]


def test_a_dismissed_finding_comes_back_when_its_cause_changes(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="ngc")])
    findings.dismiss(_open()[0]["id"])

    # The header now says Wii: a different cause, so the person decides again.
    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="wii")])

    rows = _open()
    assert [(r["image_key"], r["dismissed"]) for r in rows] == [("ps2/a.iso", False)]
    assert "wii" in rows[0]["reason"]


def test_a_reworded_reason_alone_does_not_bring_a_finding_back(db: Path) -> None:
    from dataclasses import replace

    from services import findings

    seen = _conflict("ps2/a.iso")
    findings.reconcile("folder:1", [seen])
    findings.dismiss(_open()[0]["id"])

    findings.reconcile("folder:1", [replace(seen, reason="worded differently")])

    assert _open() == []


def test_dismissing_an_unknown_finding_says_so(db: Path) -> None:
    from services import findings

    assert findings.dismiss(12345) is False


def test_forgetting_a_library_drops_its_findings(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    findings.reconcile("folder:2", [_conflict("ps2/a.iso")])
    findings.forget_library("folder:1")

    assert [r["library_id"] for r in _open()] == ["folder:2"]
