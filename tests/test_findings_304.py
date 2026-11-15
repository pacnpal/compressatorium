"""Platform mapping and the Findings view (#304).

A mapping is the user saying "this folder is PSP". It is stored per Library,
keyed by the folder's path relative to the Library root, and is a source of
evidence inside Platform detection: the header first, then the nearest folder
above the Image with a mapping or a Platform name (on the same folder the
mapping wins). A header that disagrees with that folder is a conflict.

Findings are what the folder Library's walk noticed and a person has to decide
on: a Platform conflict, or an Unsorted Image. One open Finding per kind and
Image, upserted on every walk and cleared when the cause goes away. Dismissing
one hides it until its cause changes. Nothing here moves or deletes a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services import platform_detect

from .test_folder_library_303 import (  # noqa: F401
    _IS_ROOT,
    _db_fixture,
    _register,
    _tree,
)
from .test_platform_detect import _blank, _gamecube, _ps2


def _map(path: Path, platform: str | None):
    from services import platform_mappings

    return platform_mappings.set_mapping(str(path), platform)


# ----------------------------------------------------------------------
# precedence: the header, then the nearest folder with a mapping or a name
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


# Nearest evidence wins (fix round 1, ruling a): walking up from the Image's
# folder, the first folder with a mapping or a Platform name decides; at the
# same folder the mapping wins; the header still beats both.


@pytest.mark.asyncio
async def test_a_nearer_platform_folder_beats_a_mapping_further_up(db: Path) -> None:
    image = _blank(db / "roms" / "ps2" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms", "psp")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how, found.mapped) == ("ps2", "folder", None)


@pytest.mark.asyncio
async def test_a_header_under_a_nearer_platform_folder_is_no_conflict(db: Path) -> None:
    image = _ps2(db / "roms" / "ps2" / "Ico.iso")
    await _register(db / "roms")
    _map(db / "roms", "psp")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how) == ("ps2", "header")


@pytest.mark.asyncio
async def test_a_header_is_compared_with_the_deciding_folder(db: Path) -> None:
    image = _gamecube(db / "roms" / "ps2" / "Melee.iso")
    await _register(db / "roms")
    _map(db / "roms", "psp")

    found = platform_detect.detect_platform(image)

    assert found.how == "conflict"
    assert found.reason == "in the ps2 folder, but the header says ngc"


@pytest.mark.asyncio
async def test_a_mapping_further_up_names_an_image_in_an_unnamed_folder(db: Path) -> None:
    image = _blank(db / "roms" / "misc" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms", "psp")

    assert platform_detect.detect_platform(image).platform == "psp"


@pytest.mark.asyncio
async def test_a_mapping_below_a_platform_folder_wins(db: Path) -> None:
    image = _blank(db / "roms" / "ps2" / "mine" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms" / "ps2" / "mine", "psp")

    found = platform_detect.detect_platform(image)

    assert (found.platform, found.how) == ("psp", "mapping")


@pytest.mark.asyncio
async def test_two_mapped_folders_of_the_same_name_key_apart(db: Path) -> None:
    """The cache key carries the deciding folder's path, not its name."""
    from services import platform_mappings

    image = _blank(db / "roms" / "x" / "x" / "Game.iso")
    await _register(db / "roms")
    _map(db / "roms" / "x", "psp")
    first = platform_detect.detection_key(image, Path(image).stat())
    _map(db / "roms" / "x", None)
    _map(db / "roms" / "x" / "x", "psp")
    platform_mappings.invalidate()

    assert platform_detect.detection_key(image, Path(image).stat()) != first


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


def test_a_dismissed_finding_can_be_shown_again(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    finding_id = _open()[0]["id"]
    findings.dismiss(finding_id)

    assert findings.undismiss(finding_id) is True
    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])

    assert [(r["id"], r["dismissed"]) for r in _open()] == [(finding_id, False)]
    assert findings.undismiss(12345) is False


def test_a_cause_that_changes_back_stays_shown(db: Path) -> None:
    """Dismissed under A, shown under B, back to A: still shown (its cause changed twice)."""
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="ngc")])
    findings.dismiss(_open()[0]["id"])
    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="wii")])
    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="ngc")])

    assert [r["dismissed"] for r in _open(include_dismissed=True)] == [False]


def test_forgetting_a_library_drops_its_findings(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    findings.reconcile("folder:2", [_conflict("ps2/a.iso")])
    findings.forget_library("folder:1")

    assert [r["library_id"] for r in _open()] == ["folder:2"]


# ----------------------------------------------------------------------
# the folder Library's walk produces them
# ----------------------------------------------------------------------


def _kinds() -> dict[str, str]:
    return {r["image_key"]: r["kind"] for r in _open()}


@pytest.mark.asyncio
async def test_a_walk_records_a_conflict_and_the_unsorted_images(tree: Path) -> None:
    from services import libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)

    expected = {
        "ps2/Misfiled.iso": "platform_conflict",
        "Mystery.iso": "unsorted",
    }
    if not _IS_ROOT:
        # A file the walk cannot read is an Image whose Platform nobody knows.
        expected["gc/Locked.iso"] = "unsorted"
    assert _kinds() == expected
    rows = {r["image_key"]: r for r in _open()}
    assert rows["ps2/Misfiled.iso"]["reason"] == "in the ps2 folder, but the header says ngc"
    assert rows["ps2/Misfiled.iso"]["library_id"] == library.id
    assert rows["ps2/Misfiled.iso"]["path"] == str(tree / "ps2" / "Misfiled.iso")
    if not _IS_ROOT:
        assert "could not be read" in rows["gc/Locked.iso"]["reason"]


@pytest.mark.asyncio
async def test_a_listing_served_from_the_last_walk_writes_nothing(tree: Path, monkeypatch) -> None:
    from services import findings, libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    calls = []
    monkeypatch.setattr(findings, "reconcile", lambda *a, **k: calls.append(a))

    library.summary()

    assert calls == []


@pytest.mark.asyncio
async def test_a_conflict_clears_once_the_image_is_where_it_belongs(tree: Path) -> None:
    from services import libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    assert "ps2/Misfiled.iso" in _kinds()

    # The person moved it; the app never does.
    (tree / "ps2" / "Misfiled.iso").rename(tree / "gc" / "Misfiled.iso")
    library.summary(refresh=True)

    assert "ps2/Misfiled.iso" not in _kinds()
    assert "gc/Misfiled.iso" not in _kinds()


@pytest.mark.asyncio
async def test_setting_a_mapping_clears_an_unsorted_finding(tree: Path) -> None:
    from services import libraries

    _blank(tree / "misc" / "Loose.iso")
    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    assert _kinds()["misc/Loose.iso"] == "unsorted"

    _map(tree / "misc", "psp")
    library.summary()

    assert "misc/Loose.iso" not in _kinds()


@pytest.mark.asyncio
async def test_a_mapping_that_disagrees_with_a_header_is_a_finding(tree: Path) -> None:
    from services import libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    _map(tree / "gc", "wii")
    library.summary(refresh=True)

    rows = {r["image_key"]: r for r in _open()}
    assert rows["gc/Melee.iso"]["kind"] == "platform_conflict"
    assert rows["gc/Melee.iso"]["cause"]["mapped"] == "wii"
    assert "you set the gc folder to wii" in rows["gc/Melee.iso"]["reason"]


@pytest.mark.skipif(_IS_ROOT, reason="root reads a folder whatever its mode")
@pytest.mark.asyncio
async def test_a_subfolder_the_walk_cannot_read_keeps_its_findings(tree: Path) -> None:
    from services import findings, libraries

    _gamecube(tree / "shelf" / "ps2" / "Wrong.iso")
    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    wrong = next(r for r in _open() if r["image_key"] == "shelf/ps2/Wrong.iso")
    findings.dismiss(wrong["id"])
    shelf = tree / "shelf"

    shelf.chmod(0)
    try:
        library.summary(refresh=True)
        kept = {r["image_key"]: r for r in _open(include_dismissed=True)}
        # Not seen is not gone: the row and its dismissal stay.
        assert kept["shelf/ps2/Wrong.iso"]["dismissed"] is True
        # The rest of the Library was still walked and reconciled.
        assert "ps2/Misfiled.iso" in kept
    finally:
        shelf.chmod(0o755)

    library.summary(refresh=True)
    after = {r["image_key"]: r for r in _open(include_dismissed=True)}
    assert after["shelf/ps2/Wrong.iso"]["id"] == wrong["id"]
    assert after["shelf/ps2/Wrong.iso"]["dismissed"] is True


def test_reconcile_leaves_rows_under_an_unseen_folder(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("a/x.iso"), _conflict("ab/y.iso")])
    findings.reconcile("folder:1", [], keep_under=["a"])

    # "ab" is not under "a": only the unseen folder's rows survive.
    assert [r["image_key"] for r in _open()] == ["a/x.iso"]


@pytest.mark.asyncio
async def test_a_root_that_is_gone_clears_nothing(tree: Path) -> None:
    from services import libraries
    from services.libraries.folder import RootUnavailable

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    before = _kinds()
    tree.rename(tree.parent / "elsewhere")

    with pytest.raises(RootUnavailable):
        library.summary(refresh=True)

    assert _kinds() == before


@pytest.mark.asyncio
async def test_a_finding_never_moves_or_deletes_an_image(tree: Path) -> None:
    from services import libraries

    await _register(tree)
    before = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*"))
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    from services import findings

    for row in _open():
        findings.dismiss(row["id"])
    library.summary(refresh=True)

    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")) == before


@pytest.mark.asyncio
async def test_removing_a_library_drops_its_findings_and_mappings(tree: Path) -> None:
    from services import libraries, platform_mappings

    await _register(tree)
    library = libraries.folder_libraries()[0]
    _map(tree / "gc", "ngc")
    library.summary(refresh=True)
    assert _open()

    await libraries.remove(library.id)

    assert _open() == []
    assert platform_mappings.mappings() == []


# ----------------------------------------------------------------------
# the API
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_findings_route_lists_open_findings_with_their_library(tree: Path) -> None:
    from routes import findings as routes

    library = await _register(tree)
    await routes.check_findings()

    listed = await routes.list_findings()

    rows = {r["image_key"]: r for r in listed["findings"]}
    misfiled = rows["ps2/Misfiled.iso"]
    assert misfiled["library_name"] == library["name"]
    assert misfiled["kind"] == "platform_conflict"
    # What the actions need: the folder to open, and the folder to map.
    assert misfiled["folder"] == str(tree / "ps2")
    assert misfiled["first_seen"] and misfiled["last_seen"]
    assert listed["dismissed"] == 0


@pytest.mark.asyncio
async def test_dismissing_through_the_route_hides_the_finding(tree: Path) -> None:
    from fastapi import HTTPException
    from routes import findings as routes

    await _register(tree)
    await routes.check_findings()
    first = (await routes.list_findings())["findings"][0]

    await routes.dismiss_finding(first["id"])

    listed = await routes.list_findings()
    assert first["id"] not in [r["id"] for r in listed["findings"]]
    assert listed["dismissed"] == 1
    with pytest.raises(HTTPException) as err:
        await routes.dismiss_finding(99999)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_showing_a_dismissed_finding_again_through_the_route(tree: Path) -> None:
    from fastapi import HTTPException
    from routes import findings as routes

    await _register(tree)
    await routes.check_findings()
    first = (await routes.list_findings())["findings"][0]
    await routes.dismiss_finding(first["id"])

    await routes.undismiss_finding(first["id"])

    listed = await routes.list_findings()
    assert first["id"] in [r["id"] for r in listed["findings"]]
    assert listed["dismissed"] == 0
    with pytest.raises(HTTPException) as err:
        await routes.undismiss_finding(99999)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_setting_a_mapping_through_the_route_updates_the_findings(tree: Path) -> None:
    from routes import findings as routes

    _blank(tree / "misc" / "Loose.iso")
    library = await _register(tree)
    await routes.check_findings()
    assert "misc/Loose.iso" in {
        r["image_key"] for r in (await routes.list_findings())["findings"]
    }

    saved = await routes.set_platform_mapping(
        routes.MappingRequest(path=str(tree / "misc"), platform="psp"),
    )

    assert (saved["library_id"], saved["folder"], saved["platform"]) == (
        library["id"], "misc", "psp",
    )
    assert "misc/Loose.iso" not in {
        r["image_key"] for r in (await routes.list_findings())["findings"]
    }
    listed = await routes.list_platform_mappings()
    assert [(m["folder"], m["platform"]) for m in listed] == [("misc", "psp")]


@pytest.mark.asyncio
async def test_a_mapping_outside_a_library_is_refused_by_the_route(db: Path) -> None:
    from fastapi import HTTPException
    from routes import findings as routes

    (db / "loose").mkdir()
    with pytest.raises(HTTPException) as err:
        await routes.set_platform_mapping(
            routes.MappingRequest(path=str(db / "loose"), platform="psp"),
        )
    assert err.value.status_code == 404


def test_the_platform_picker_lists_known_platforms() -> None:
    from routes import findings as routes

    listed = routes.list_platforms()

    assert {"slug": "psp", "name": "PlayStation Portable"} in listed
    assert len({p["slug"] for p in listed}) == len(listed)


# ----------------------------------------------------------------------
# a walk that outlives its Library (I2)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_walk_that_finishes_after_its_library_is_removed_writes_nothing(
    tree: Path,
) -> None:
    from services import libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    await libraries.remove(library.id)

    # A walk still running when the Library went (a listing, Check now, a
    # walk stranded on a dead mount) finishes now.
    library.summary(refresh=True)

    assert _open() == []


def test_reconcile_writes_nothing_once_the_producer_is_gone(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")], current=lambda: False)

    assert _open() == []


# ----------------------------------------------------------------------
# a mapping set while a walk is running (I3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_walk_that_saw_a_mapping_change_discards_its_findings(
    tree: Path, monkeypatch,
) -> None:
    from services import libraries
    from services.libraries.folder import FolderLibrary

    _blank(tree / "misc" / "Loose.iso")
    await _register(tree)
    library = libraries.folder_libraries()[0]
    real = FolderLibrary._findings
    changed = {"done": False}

    def mapping_lands_mid_walk(self, scan):
        # The PUT arrives after this walk detected Loose.iso as Unsorted.
        if not changed["done"]:
            changed["done"] = True
            _map(tree / "misc", "psp")
        return real(self, scan)

    scheduled = []
    monkeypatch.setattr(FolderLibrary, "_findings", mapping_lands_mid_walk)
    monkeypatch.setattr(FolderLibrary, "_schedule_rewalk", lambda self: scheduled.append(self))

    library.summary(refresh=True)

    # Nothing written from the stale walk, and a fresh walk is asked for.
    assert _open() == []
    assert scheduled == [library]

    library.summary(refresh=True)
    assert "misc/Loose.iso" not in _kinds()
    assert "ps2/Misfiled.iso" in _kinds()


@pytest.mark.asyncio
async def test_a_scheduled_rewalk_runs_on_its_own_thread(tree: Path) -> None:
    from services import libraries

    _blank(tree / "misc" / "Loose.iso")
    await _register(tree)
    library = libraries.folder_libraries()[0]

    library._schedule_rewalk()
    library._rewalk_thread.join(timeout=30)

    assert _kinds()["misc/Loose.iso"] == "unsorted"


@pytest.mark.asyncio
async def test_a_mapping_put_during_a_walk_says_findings_follow(tree: Path) -> None:
    from routes import findings as routes
    from services import libraries

    (tree / "misc").mkdir()
    await _register(tree)
    library = libraries.folder_libraries()[0]

    library._lock.acquire()  # a sweep or listing is mid-walk
    try:
        saved = await routes.set_platform_mapping(
            routes.MappingRequest(path=str(tree / "misc"), platform="psp"),
        )
    finally:
        library._lock.release()

    assert saved["platform"] == "psp"
    assert saved["walk"]["status"] == "listing_in_progress"
    assert saved["walk"]["message"] == "A listing is running; Findings refresh after it."


# ----------------------------------------------------------------------
# minor fixes from review round 1
# ----------------------------------------------------------------------

_CUE = 'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'


@pytest.mark.skipif(_IS_ROOT, reason="root reads a file whatever its mode")
@pytest.mark.asyncio
async def test_an_unreadable_track_of_a_sheet_is_not_an_image_finding(tree: Path) -> None:
    from services import libraries

    sheet = tree / "sheet"
    sheet.mkdir()
    (sheet / "Game.cue").write_text(_CUE)
    track = sheet / "Game.bin"
    track.write_bytes(b"\0" * 2352)
    track.chmod(0)
    try:
        await _register(tree)
        libraries.folder_libraries()[0].summary(refresh=True)
    finally:
        track.chmod(0o644)

    # The track belongs to Game.cue; it is not an Image of its own (CONTEXT.md).
    assert "sheet/Game.bin" not in _kinds()


def test_a_headers_disagree_conflict_names_the_headers_in_its_cause(db: Path) -> None:
    from services.libraries.folder import Entry, FolderLibrary, Scan
    from services.platform_detect import Detection

    library = FolderLibrary(record_id=1, root=str(db), name="r")
    found = Detection(
        None, "conflict", None, None, "headers disagree (a=ngc, b=ps2)",
        None, ("ngc", "ps2"),
    )
    entry = Entry(key="x.iso", path=str(db / "x.iso"), size=1, mtime=0.0, detection=found)

    observed = library._findings(Scan(entries=[entry]))

    assert observed[0].cause["headers"] == ["ngc", "ps2"]


@pytest.mark.asyncio
async def test_a_sheet_whose_track_is_outside_the_library_shows_no_outside_path(
    db: Path,
) -> None:
    """With #303's escaping-track fix merged: no Finding shows an outside path.

    One sheet points outside the Library root (still inside the Volume), one
    outside the Volume altogether, which #303 refuses. Whatever the walk makes
    of either, the Finding it records must not carry the outside path:
    ``reason`` and ``cause`` are served by /api/findings and shown in the UI.
    """
    from services import libraries

    beside = db / "beside"
    beside.mkdir()
    (beside / "neighbour.bin").write_bytes(b"\0" * 2352)
    beyond = db.parent / "beyond"
    beyond.mkdir()
    (beyond / "secret.bin").write_bytes(b"\0" * 2352)
    roms = db / "roms"
    roms.mkdir()
    (roms / "Beside.cue").write_text(_CUE.replace("Game.bin", "../beside/neighbour.bin"))
    (roms / "Beyond.cue").write_text(_CUE.replace("Game.bin", "../../beyond/secret.bin"))
    await _register(roms)

    libraries.folder_libraries()[0].summary(refresh=True)

    rows = _open(include_dismissed=True)
    # Both sheets are Images nobody can place, so both are Findings.
    assert {r["image_key"] for r in rows} == {"Beside.cue", "Beyond.cue"}
    for row in rows:
        text = f"{row['reason']} {row['cause']}"
        for leak in ("beside", "neighbour", "beyond", "secret", str(db.parent)):
            assert leak not in text, (leak, row)


# ----------------------------------------------------------------------
# a dismissal outlives a walk that did not see its Image (review r2, F3)
# ----------------------------------------------------------------------


def _all() -> dict[str, dict]:
    return {r["image_key"]: r for r in _open(include_dismissed=True)}


@pytest.mark.skipif(_IS_ROOT, reason="root reads a file whatever its mode")
@pytest.mark.asyncio
async def test_a_dismissed_conflict_unreadable_for_one_walk_stays_dismissed(
    tree: Path,
) -> None:
    from services import findings, libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    first = _all()["ps2/Misfiled.iso"]
    findings.dismiss(first["id"])
    misfiled = tree / "ps2" / "Misfiled.iso"

    misfiled.chmod(0)
    try:
        library.summary(refresh=True)
        # Not read is not changed: the row is kept as it was.
        mid = _all()["ps2/Misfiled.iso"]
        assert (mid["id"], mid["kind"], mid["dismissed"]) == (
            first["id"], "platform_conflict", True,
        )
    finally:
        misfiled.chmod(0o644)

    library.summary(refresh=True)
    after = _all()["ps2/Misfiled.iso"]
    assert (after["kind"], after["dismissed"]) == ("platform_conflict", True)


@pytest.mark.asyncio
async def test_a_dismissed_conflict_under_an_empty_mount_point_comes_back_dismissed(
    tree: Path,
) -> None:
    import shutil

    from services import findings, libraries

    _gamecube(tree / "usb" / "ps2" / "Wrong.iso")
    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    findings.dismiss(_all()["usb/ps2/Wrong.iso"]["id"])
    usb = tree / "usb"
    stash = tree.parent / "stash"

    # An unmounted mount point is an empty folder: it lists without error.
    shutil.move(str(usb), str(stash))
    usb.mkdir()
    library.summary(refresh=True)
    # The row goes (rule 3), and the dismissal does not.
    assert "usb/ps2/Wrong.iso" not in _all()
    usb.rmdir()
    shutil.move(str(stash), str(usb))
    library.summary(refresh=True)

    after = _all()["usb/ps2/Wrong.iso"]
    assert (after["kind"], after["dismissed"]) == ("platform_conflict", True)


@pytest.mark.skipif(_IS_ROOT, reason="root reads a file whatever its mode")
@pytest.mark.asyncio
async def test_a_dismissed_unsorted_image_unreadable_for_one_walk_stays_dismissed(
    tree: Path,
) -> None:
    from services import findings, libraries

    await _register(tree)
    library = libraries.folder_libraries()[0]
    library.summary(refresh=True)
    first = _all()["Mystery.iso"]
    findings.dismiss(first["id"])
    mystery = tree / "Mystery.iso"

    mystery.chmod(0)
    try:
        library.summary(refresh=True)
        mid = _all()["Mystery.iso"]
        assert (mid["id"], mid["reason"], mid["dismissed"]) == (
            first["id"], first["reason"], True,
        )
    finally:
        mystery.chmod(0o644)

    library.summary(refresh=True)
    after = _all()["Mystery.iso"]
    assert (after["id"], after["dismissed"]) == (first["id"], True)


def test_a_finding_that_comes_back_unchanged_comes_back_dismissed(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    findings.dismiss(_open()[0]["id"])
    findings.reconcile("folder:1", [])  # the walk did not see it
    assert _open(include_dismissed=True) == []

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])

    assert [r["dismissed"] for r in _open(include_dismissed=True)] == [True]


def test_showing_again_forgets_the_dismissal_for_good(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    finding_id = _open()[0]["id"]
    findings.dismiss(finding_id)
    findings.undismiss(finding_id)
    findings.reconcile("folder:1", [])
    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])

    assert [r["dismissed"] for r in _open()] == [False]


def test_a_dismissal_is_kept_per_kind_and_cause(db: Path) -> None:
    from services import db as _db
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso", header="ngc")])
    findings.dismiss(_open()[0]["id"])

    with _db.SessionLocal() as session:
        rows = session.query(_db.FindingDismissalRecord).all()
        keys = [(r.library_id, r.image_key, r.kind, r.fingerprint) for r in rows]
    assert keys == [
        ("folder:1", "ps2/a.iso", "platform_conflict",
         _conflict("ps2/a.iso", header="ngc").fingerprint),
    ]


def test_forgetting_a_library_drops_its_dismissals(db: Path) -> None:
    from services import findings

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])
    findings.dismiss(_open()[0]["id"])
    findings.forget_library("folder:1")

    findings.reconcile("folder:1", [_conflict("ps2/a.iso")])

    assert [r["dismissed"] for r in _open()] == [False]


@pytest.mark.asyncio
async def test_a_conflict_names_the_folder_that_decided(tree: Path) -> None:
    """The cause carries the deciding folder, not the nearest Platform name."""
    from services import libraries

    _gamecube(tree / "ps2" / "mine" / "Melee.iso")
    await _register(tree)
    _map(tree / "ps2" / "mine", "psp")
    libraries.folder_libraries()[0].summary(refresh=True)

    rows = _all()
    mapped = rows["ps2/mine/Melee.iso"]["cause"]
    assert (mapped["folder"], mapped["folder_platform"], mapped["mapped"]) == (
        "ps2/mine", "psp", "psp",
    )
    named = rows["ps2/Misfiled.iso"]["cause"]
    assert (named["folder"], named["folder_platform"], named["mapped"]) == ("ps2", "ps2", None)

