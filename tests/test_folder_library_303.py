"""Folder Libraries (#303): a folder inside a Volume, registered as a Library.

The folder Library is the second Library behind the Rule engine (#296). Its
Platforms come from Platform detection (#294), its Image key is the path
relative to the Library root, and its Rules support the skip policy only. The
sweep tests run on a temp tree with ``roms/ps2``, ``roms/gc``, a misfiled Image
and an unreadable one. Nothing runs a binary: tool readiness and the queue are
stubbed, as the other sweep tests do.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from services import db as _db
from services import platform_detect
from services.job_manager import job_manager
from services.preferences_store import preferences_store
from services.tools import registry

from config import settings as app_settings

from .test_platform_detect import _blank, _gamecube, _ps2

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(name="db")
def _db_fixture(tmp_path: Path, monkeypatch):
    from services import libraries
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    platform_detect.clear_cache()
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(app_settings, "chd_volumes", str(volume))
    yield volume
    platform_detect.clear_cache()
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest.fixture(name="tree")
def _tree(db: Path):
    """``roms/ps2``, ``roms/gc``, a misfiled Image, an Unsorted one, an unreadable one."""
    roms = db / "roms"
    _ps2(roms / "ps2" / "Ico.iso")
    _gamecube(roms / "gc" / "Melee.iso")
    _gamecube(roms / "gc" / "Sunshine.iso")
    # A GameCube disc filed under ps2: header and folder disagree.
    _gamecube(roms / "ps2" / "Misfiled.iso")
    # Nothing identifies it.
    _blank(roms / "Mystery.iso")
    # Not an Image at all.
    (roms / "gc" / "notes.txt").write_text("hello")
    locked = Path(_gamecube(roms / "gc" / "Locked.iso"))
    locked.chmod(0)
    yield roms
    if locked.exists():
        locked.chmod(0o644)


@pytest.fixture(name="ready")
def _ready(monkeypatch):
    async def always(_tool):
        return True

    monkeypatch.setattr(registry, "tool_is_ready", always)


@pytest.fixture(name="queue")
def _queue(monkeypatch):
    """The queue stubbed: records what a run queued, and reports it in flight."""
    queued: list[str] = []

    async def fake_batch(paths, mode, **_kwargs):
        jobs = []
        for path in paths:
            queued.append(path)
            jobs.append(SimpleNamespace(file_path=path, id=f"job-{len(queued)}"))
        return jobs

    monkeypatch.setattr(job_manager, "create_batch_jobs", fake_batch)
    monkeypatch.setattr(
        job_manager, "get_active_job_candidates",
        lambda: [(f"job-{i}", [p]) for i, p in enumerate(queued)],
    )
    return queued


async def _register(path: Path, **extra):
    from routes import libraries as routes

    return await routes.register_library(
        routes.RegisterLibraryRequest(path=str(path), **extra),
    )


# ----------------------------------------------------------------------
# registering and removing
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_folder_inside_a_volume_registers_as_a_library(tree: Path) -> None:
    from routes import libraries as routes

    created = await _register(tree)

    assert created["kind"] == "folder"
    assert created["id"].startswith("folder:")
    assert created["root"] == str(tree.resolve())
    assert created["name"] == "roms"
    listed = await routes.list_libraries()
    assert [lib["id"] for lib in listed] == ["romm", created["id"]]
    # Stored in the database, so a restart still has it.
    from services import libraries

    libraries.reset_for_tests()
    assert [lib["id"] for lib in await routes.list_libraries()] == ["romm", created["id"]]


@pytest.mark.asyncio
async def test_a_folder_outside_the_volumes_is_refused(db: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(HTTPException) as exc:
        await _register(outside)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_missing_folder_or_a_file_is_refused(tree: Path) -> None:
    with pytest.raises(HTTPException) as missing:
        await _register(tree / "nope")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as a_file:
        await _register(tree / "Mystery.iso")
    assert a_file.value.status_code == 404


@pytest.mark.asyncio
async def test_the_same_or_an_overlapping_folder_is_refused(tree: Path) -> None:
    await _register(tree / "gc")

    for path in (tree / "gc", tree, tree / "gc" / "."):
        with pytest.raises(HTTPException) as exc:
            await _register(path)
        assert exc.value.status_code == 409, path


@pytest.mark.asyncio
async def test_removing_a_library_drops_its_rules_and_state_and_keeps_files(
    tree: Path,
) -> None:
    from routes import libraries as routes
    from routes import romm as romm_routes

    await romm_routes.put_romm_rules({"7": {"mode": "dolphin_rvz"}})
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ngc": {"mode": "dolphin_rvz"}}})
    await preferences_store.put(
        "rules.state",
        {**(await preferences_store.get("rules.state") or {}),
         f"{lib['id']}:ngc": {"last_queued": 1}},
    )
    before = sorted(str(p) for p in tree.rglob("*"))

    await routes.remove_library(lib["id"])

    assert [x["id"] for x in await routes.list_libraries()] == ["romm"]
    assert set(await preferences_store.get("rules")) == {"romm:7"}
    assert not any(k.startswith(lib["id"]) for k in await preferences_store.get("rules.state"))
    assert sorted(str(p) for p in tree.rglob("*")) == before
    with pytest.raises(HTTPException) as exc:
        await routes.get_library_rules(lib["id"])
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_romm_library_cannot_be_removed(db: Path) -> None:
    from routes import libraries as routes

    with pytest.raises(HTTPException) as exc:
        await routes.remove_library("romm")
    assert exc.value.status_code == 400


# ----------------------------------------------------------------------
# what a Library shows
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_library_shows_its_platforms_with_counts(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    summary = await routes.library_platforms(lib["id"])

    counts = {p["id"]: p["images"] for p in summary["platforms"]}
    readable_gc = 2 if not _IS_ROOT else 3
    assert counts == {"ps2": 1, "ngc": readable_gc}
    assert summary["unsorted"] == 1
    assert summary["conflicted"] == 1
    if not _IS_ROOT:
        assert summary["unreadable"] == 1
    # The editor narrows modes the same way RomM's platform list does.
    ngc = next(p for p in summary["platforms"] if p["id"] == "ngc")
    assert "dolphin_rvz" in ngc["mode_ids"]
    assert "createdvd" not in ngc["mode_ids"]


@pytest.mark.asyncio
async def test_an_image_key_is_its_path_relative_to_the_root(tree: Path) -> None:
    from services import libraries

    lib = await _register(tree)
    library = libraries.get(lib["id"])

    images = {i.key: i for i in library.images("ngc")}
    assert set(images) == (
        {"gc/Melee.iso", "gc/Sunshine.iso"} if not _IS_ROOT
        else {"gc/Melee.iso", "gc/Sunshine.iso", "gc/Locked.iso"}
    )
    melee = images["gc/Melee.iso"]
    assert melee.name == "Melee.iso"
    assert melee.platform == "ngc"
    assert melee.path == str(tree.resolve() / "gc" / "Melee.iso")
    assert melee.size == (tree / "gc" / "Melee.iso").stat().st_size


@pytest.mark.asyncio
async def test_recognized_reads_the_cached_dat_or_hasheous_match(tree: Path) -> None:
    from services import libraries
    from services.dat_store import DATStore

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    store = DATStore()
    root = tree.resolve()
    await store.set_match(str(root / "gc" / "Melee.iso"), {
        "matched": True, "game_name": "Melee", "match_type": "file_sha1",
    })
    await store.set_match(str(root / "gc" / "Sunshine.iso"), {
        "matched": True, "source": "hasheous", "game_name": "Sunshine",
    })
    await store.set_match(str(root / "ps2" / "Ico.iso"), {"matched": False})

    recognized = {i.key: i.recognized for i in library.images("ngc")}
    recognized |= {i.key: i.recognized for i in library.images("ps2")}

    assert recognized["gc/Melee.iso"] is True
    assert recognized["gc/Sunshine.iso"] is True
    assert recognized["ps2/Ico.iso"] is False


@pytest.mark.asyncio
async def test_an_image_never_matched_is_unknown(tree: Path) -> None:
    """No match was ever computed: Recognized is unknown, not False."""
    from services import libraries

    lib = await _register(tree)
    images = libraries.get(lib["id"]).images("ngc")
    assert {i.recognized for i in images} == {None}


# ----------------------------------------------------------------------
# Rules: same settings as RomM's, skip policy only
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_folder_rules_offer_only_the_skip_policy(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    got = await routes.get_library_rules(lib["id"])

    assert got["options"]["duplicate_actions"] == ["skip"]
    assert got["defaults"]["duplicate_action"] == "skip"
    # Every other setting a RomM Rule has.
    from services.romm import auto as romm_auto

    assert set(got["defaults"]) == set(romm_auto.default_rule())


@pytest.mark.asyncio
async def test_overwrite_and_rename_are_refused_for_a_folder_rule(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    for policy in ("overwrite", "rename"):
        with pytest.raises(HTTPException) as exc:
            await routes.put_library_rules(lib["id"], {"rules": {
                "ngc": {"mode": "dolphin_rvz", "duplicate_action": policy},
            }})
        assert exc.value.status_code == 422
    assert (await routes.get_library_rules(lib["id"]))["rules"] == {}


@pytest.mark.asyncio
async def test_a_stored_overwrite_rule_reads_back_as_skip(tree: Path) -> None:
    """A blob written straight into the database cannot widen the policy."""
    from routes import libraries as routes

    lib = await _register(tree)
    await preferences_store.put("rules", {
        f"{lib['id']}:ngc": {"mode": "dolphin_rvz", "duplicate_action": "overwrite"},
    })

    got = await routes.get_library_rules(lib["id"])
    assert got["rules"]["ngc"]["duplicate_action"] == "skip"


@pytest.mark.asyncio
async def test_romm_rules_keep_every_policy(db: Path) -> None:
    from routes import romm as romm_routes

    saved = await romm_routes.put_romm_rules({
        "7": {"mode": "dolphin_rvz", "duplicate_action": "overwrite"},
    })
    assert saved["rules"]["7"]["duplicate_action"] == "overwrite"
    got = await romm_routes.get_romm_rules()
    assert got["options"]["duplicate_actions"] == ["skip", "overwrite", "rename"]


@pytest.mark.asyncio
async def test_a_rule_names_a_platform_slug(tree: Path) -> None:
    from routes import libraries as routes

    lib = await _register(tree)
    saved = await routes.put_library_rules(lib["id"], {"rules": {
        "gc": {"mode": "dolphin_rvz"},  # an alias, stored as the slug
        "not-a-platform": {"mode": "dolphin_rvz"},
        "a:b": {"mode": "dolphin_rvz"},
    }})
    assert list(saved["rules"]) == ["ngc"]


# ----------------------------------------------------------------------
# sweeps on the temp tree
# ----------------------------------------------------------------------


_RULES = {
    "ps2": {"mode": "createdvd", "enabled": True},
    "ngc": {"mode": "dolphin_rvz", "enabled": True},
}


async def _library_with_rules(tree: Path) -> dict:
    from routes import libraries as routes

    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": _RULES})
    return lib


@pytest.mark.asyncio
async def test_a_sweep_queues_only_images_with_a_platform(
    tree: Path, ready, queue,
) -> None:
    """The misfiled (Conflicted), Unsorted and unreadable Images are never queued."""
    from routes import libraries as routes

    lib = await _library_with_rules(tree)
    result = await routes.run_library(lib["id"])

    names = sorted(Path(p).name for p in queue)
    expected = ["Ico.iso", "Melee.iso", "Sunshine.iso"]
    if _IS_ROOT:
        expected = ["Ico.iso", "Locked.iso", "Melee.iso", "Sunshine.iso"]
    assert names == expected, result
    assert result["queued"] == len(expected)
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_running_a_sweep_twice_queues_nothing_new(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    from routes import libraries as routes

    (tree / "gc" / "Locked.iso").unlink()
    outputs: list[str] = []
    real_batch = job_manager.create_batch_jobs

    async def batch_and_note_outputs(paths, mode, **kwargs):
        outputs.extend(kwargs["output_paths"][p] for p in paths)
        return await real_batch(paths, mode, **kwargs)

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch_and_note_outputs)
    lib = await _library_with_rules(tree)

    first = await routes.run_library(lib["id"])
    second = await routes.run_library(lib["id"])
    assert first["queued"] == 3
    # Still in the queue: nothing is queued twice.
    assert second["queued"] == 0
    assert second["skipped_active"] == 3

    # The jobs finish: their outputs exist and the queue is empty again.
    for output in outputs:
        Path(output).write_bytes(b"done")
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)
    third = await routes.run_library(lib["id"])
    assert third["queued"] == 0
    # The three Sources, plus the two RVZ outputs, which are ngc Images too.
    assert third["skipped_existing"] == 5
    assert len(queue) == 3


@pytest.mark.asyncio
async def test_preview_does_not_move_the_schedule_clock(
    tree: Path, ready, queue,
) -> None:
    from routes import libraries as routes

    (tree / "gc" / "Locked.iso").unlink()
    lib = await _library_with_rules(tree)

    preview = await routes.preview_library(lib["id"])
    assert preview["dry_run"] is True
    assert preview["queued"] == 3
    assert queue == []
    assert (await routes.get_library_rules(lib["id"]))["state"] == {}

    await routes.run_library(lib["id"])
    state = (await routes.get_library_rules(lib["id"]))["state"]
    assert set(state) == {"ps2", "ngc"}
    assert all(entry["last_run_at"] for entry in state.values())


@pytest.mark.asyncio
async def test_a_sweep_can_be_narrowed_to_one_platform(
    tree: Path, ready, queue,
) -> None:
    from routes import libraries as routes

    lib = await _library_with_rules(tree)
    result = await routes.run_library(
        lib["id"], routes.SweepRequest(platform_ids=["ps2"]),
    )
    assert [Path(p).name for p in queue] == ["Ico.iso"]
    assert [p["platform_id"] for p in result["platforms"]] == ["ps2"]


@pytest.mark.asyncio
async def test_the_scheduler_sweeps_a_folder_library_whose_switch_is_on(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """One scheduler tick, with the folder Library's switch on and RomM's off."""
    import asyncio

    from routes import libraries as routes
    from services import libraries
    from services.rules import sweep as rules_sweep

    lib = await _library_with_rules(tree)
    updated = await routes.update_library(
        lib["id"], routes.UpdateLibraryRequest(schedule_enabled=True, max_per_sweep=2),
    )
    assert updated["schedule_enabled"] is True
    assert updated["max_per_sweep"] == 2

    ticks = 0

    async def one_tick(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(rules_sweep.asyncio, "sleep", one_tick)
    with pytest.raises(asyncio.CancelledError):
        await libraries.run_forever()

    # Capped by the Library's own limit.
    assert len(queue) == 2


@pytest.mark.asyncio
async def test_a_library_whose_folder_is_gone_reports_it(
    tree: Path, ready, queue,
) -> None:
    import shutil

    from routes import libraries as routes

    lib = await _library_with_rules(tree)
    (tree / "gc" / "Locked.iso").chmod(0o644)
    shutil.rmtree(tree)

    result = await routes.run_library(lib["id"])
    assert result["queued"] == 0
    assert result["stopped_reason"] == "not_configured"
    with pytest.raises(HTTPException) as exc:
        await routes.library_platforms(lib["id"])
    assert exc.value.status_code == 409
