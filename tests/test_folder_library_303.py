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
import time
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
async def test_a_sweep_and_the_walk_skip_romm_staging(
    tree: Path, ready, queue,
) -> None:
    """RomM's `.romm_tmp_*` staging is never walked or queued; a look-alike name is (#384)."""
    from routes import libraries as routes

    _gamecube(tree / "gc" / ".romm_tmp_convert_a.iso")
    _gamecube(tree / "gc" / ".romm_tmp_convert_dir" / "Staged.iso")
    _gamecube(tree / "gc" / "backup_romm_tmp_x" / "Kept.iso")
    _gamecube(tree / "gc" / "romm_tmp_Also.iso")

    lib = await _library_with_rules(tree)
    result = await routes.run_library(lib["id"])

    names = sorted(Path(p).name for p in queue)
    expected = ["Ico.iso", "Kept.iso", "Melee.iso", "Sunshine.iso", "romm_tmp_Also.iso"]
    if _IS_ROOT:
        expected = sorted([*expected, "Locked.iso"])
    assert names == expected, result
    assert not any(".romm_tmp_" in p for p in queue)


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
    # The three Sources. (The new RVZ outputs are Images too, but this sweep
    # reuses the walk the first one took, from before they existed.)
    assert third["skipped_existing"] == 3
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
    # `configured()` touches no disk (fix round 1, I3), so a missing root is
    # found by the listing, off the event loop, and reported per Platform.
    assert {e["error"] for e in result["errors"]} == {"root_unavailable"}
    with pytest.raises(HTTPException) as exc:
        await routes.library_platforms(lib["id"])
    assert exc.value.status_code == 409


# ----------------------------------------------------------------------
# fix round 1
# ----------------------------------------------------------------------


def _psx_tree(roms: Path) -> None:
    psx = roms / "psx"
    (psx / "Single").mkdir(parents=True)
    (psx / "Single" / "Game.cue").write_text(
        'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
    )
    (psx / "Single" / "Game.bin").write_bytes(b"\0" * 4096)
    (psx / "Multi").mkdir()
    (psx / "Multi" / "Multi.cue").write_text(
        'FILE "Multi (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n'
        'FILE "Multi (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n',
    )
    (psx / "Multi" / "Multi (Track 1).bin").write_bytes(b"\0" * 4096)
    (psx / "Multi" / "Multi (Track 2).bin").write_bytes(b"\0" * 4096)
    (psx / "Multi.m3u").write_text("Multi/Multi.cue\n")


@pytest.fixture(name="psx")
def _psx(db: Path) -> Path:
    """The reviewer's tree: a single-track and a multi-track cue/bin set, and a playlist."""
    roms = db / "roms"
    _psx_tree(roms)
    return roms


@pytest.mark.asyncio
async def test_a_cue_bin_set_is_one_image(psx: Path) -> None:
    from routes import libraries as routes

    lib = await _register(psx)
    summary = await routes.library_platforms(lib["id"])

    assert {p["id"]: p["images"] for p in summary["platforms"]} == {"psx": 2}
    from services import libraries

    keys = sorted(i.key for i in libraries.get(lib["id"]).images("psx"))
    assert keys == ["psx/Multi/Multi.cue", "psx/Single/Game.cue"]


@pytest.mark.asyncio
async def test_a_psx_rule_queues_the_sheet_not_its_tracks(psx: Path, ready, queue) -> None:
    from routes import libraries as routes

    lib = await _register(psx)
    await routes.put_library_rules(lib["id"], {"rules": {
        "psx": {"mode": "createcd", "enabled": True},
    }})

    result = await routes.run_library(lib["id"])

    assert sorted(Path(p).name for p in queue) == ["Game.cue", "Multi.cue"], result
    assert result["skipped_active"] == 0


@pytest.mark.asyncio
async def test_a_failed_image_waits_until_its_file_changes(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})

    await routes.run_library(lib["id"])
    ico = tree / "ps2" / "Ico.iso"
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Ico.iso"], status=JobStatus.FAILED, file_path=str(ico.resolve()),
    ))
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)

    held = await routes.run_library(lib["id"])
    assert held["queued"] == 0, held
    assert held["skipped_failed"] == 1

    # The file changes: it is tried again.
    with ico.open("ab") as fh:
        fh.write(b"\0" * 2048)
    retried = await routes.run_library(lib["id"])
    assert retried["queued"] == 1, retried


@pytest.mark.asyncio
async def test_romm_does_not_hold_failed_images(db: Path) -> None:
    """RomM's sweep re-queues a failed ROM, as it always has."""
    from services.romm.library import romm_library
    from services.rules import library as rules_library

    assert rules_library.FAILED_HOLD not in romm_library.capabilities


@pytest.mark.asyncio
async def test_an_unreadable_match_cache_leaves_recognized_unknown(
    tree: Path, monkeypatch,
) -> None:
    from services import libraries
    from services.dat_store import dat_store

    def broken(_paths):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dat_store, "get_matches_batch", broken)
    lib = await _register(tree)

    images = libraries.get(lib["id"]).images("ps2")
    assert {i.recognized for i in images} == {None}


@pytest.mark.asyncio
async def test_only_unmatched_skips_images_nobody_matched(
    tree: Path, ready, queue,
) -> None:
    """Fail closed: unknown is not "not Recognized"."""
    from routes import libraries as routes
    from services.dat_store import DATStore

    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await DATStore().set_match(str((tree / "gc" / "Melee.iso").resolve()), {"matched": False})
    await routes.put_library_rules(lib["id"], {"rules": {
        "ngc": {"mode": "dolphin_rvz", "enabled": True, "only_unmatched": True},
    }})

    result = await routes.run_library(lib["id"])

    assert [Path(p).name for p in queue] == ["Melee.iso"], result


@pytest.mark.asyncio
async def test_a_sweep_walks_the_root_once(tree: Path, ready, queue, monkeypatch) -> None:
    from routes import libraries as routes
    from services.libraries.folder import FolderLibrary

    lib = await _library_with_rules(tree)
    walks = {"n": 0}
    real_walk = FolderLibrary._walk

    def counting(self):
        walks["n"] += 1
        return real_walk(self)

    monkeypatch.setattr(FolderLibrary, "_walk", counting)

    await routes.run_library(lib["id"])
    assert walks["n"] == 1


def test_a_second_listing_reads_no_headers(db: Path, monkeypatch) -> None:
    """10,000 Images: the second walk reuses every detection (keyed by size and mtime)."""
    from services.libraries.folder import FolderLibrary

    roms = db / "roms" / "ps2"
    roms.mkdir(parents=True)
    for i in range(10_000):
        (roms / f"g{i:05d}.iso").write_bytes(b"\0" * 64)
    library = FolderLibrary(record_id=1, root=str((db / "roms").resolve()), name="r")
    decisions = {"n": 0}
    real = platform_detect._decide

    def counting(*args, **kwargs):
        decisions["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(platform_detect, "_decide", counting)

    first = library.summary(refresh=True)
    assert decisions["n"] == 10_000
    decisions["n"] = 0
    second = library.summary(refresh=True)
    assert decisions["n"] == 0
    assert first["platforms"] == second["platforms"]
    assert second["platforms"][0]["images"] == 10_000


def test_configured_touches_no_disk(db: Path, monkeypatch) -> None:
    from services.libraries.folder import FolderLibrary

    library = FolderLibrary(record_id=1, root=str(db / "roms"), name="r")
    touched: list[str] = []
    real_stat, real_isdir = os.stat, os.path.isdir

    def stat(*args, **kwargs):
        touched.append("stat")
        return real_stat(*args, **kwargs)

    def isdir(*args, **kwargs):
        touched.append("isdir")
        return real_isdir(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(os, "stat", stat)
        patched.setattr(os.path, "isdir", isdir)
        configured = library.configured()
    assert configured is True
    assert touched == []


@pytest.mark.asyncio
async def test_a_hung_listing_fails_the_sweep_and_frees_the_lock(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    import threading

    from routes import libraries as routes
    from services.libraries.folder import FolderLibrary
    from services.rules import sweep as rules_sweep

    lib = await _library_with_rules(tree)
    release = threading.Event()

    def hung(self, platform_id):
        release.wait(5)
        return []

    monkeypatch.setattr(FolderLibrary, "images", hung)
    monkeypatch.setattr(rules_sweep, "_LISTING_SECONDS", 0.2)
    try:
        started = time.monotonic()
        result = await routes.run_library(lib["id"])
        # Returned on the bound, not when the hung listing gave up (5 s).
        assert time.monotonic() - started < 2, "the sweep waited for the hung thread"
        assert result["stopped_reason"] == "library_unresponsive", result
        assert not rules_sweep._sweep_lock.locked()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_platforms_have_display_names(psx: Path) -> None:
    from routes import libraries as routes

    lib = await _register(psx)
    summary = await routes.library_platforms(lib["id"])

    assert [(p["id"], p["name"]) for p in summary["platforms"]] == [("psx", "PlayStation")]


def test_every_platform_has_a_display_name() -> None:
    names = {slug: platform_detect.platform_name(slug) for slug in platform_detect._ALIASES}
    assert all(name and name != slug for slug, name in names.items()), names
    # A slug a tool can declare without an alias (#324's eXoDOS) has one too.
    assert platform_detect.platform_name("dos") == "DOS"


@pytest.mark.asyncio
async def test_a_folder_inside_romms_library_is_refused(db: Path) -> None:
    from services.romm import settings as romm_settings

    library = db / "library"
    (library / "roms" / "gc").mkdir(parents=True)
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(library)})

    with pytest.raises(HTTPException) as exc:
        await _register(library / "roms" / "gc")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as around:
        await _register(db)
    assert around.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_nested_registrations_let_one_through(tree: Path) -> None:
    import asyncio

    outcomes = await asyncio.gather(
        # Every pair overlaps: the root, a folder in it, and that folder again.
        _register(tree), _register(tree / "gc"), _register(tree / "gc"),
        return_exceptions=True,
    )
    ok = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(ok) == 1, outcomes
    assert {e.status_code for e in refused} == {409}


@pytest.mark.asyncio
async def test_the_engine_refuses_an_unsupported_policy_on_submission(tree: Path) -> None:
    """The refusal lives in rules/, so a caller other than the API gets it too."""
    from services import libraries
    from services.rules import UnsupportedPolicy
    from services.rules import sweep as rules_sweep

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    with pytest.raises(UnsupportedPolicy) as exc:
        await rules_sweep.set_rules(
            library, {"ngc": {"mode": "dolphin_rvz", "duplicate_action": "rename"}},
            strict=True,
        )
    assert exc.value.allowed == ("skip",)
    assert await rules_sweep.get_rules(library) == {}


# ----------------------------------------------------------------------
# fix round 2
# ----------------------------------------------------------------------


def _finishes(func, seconds: float = 2.0):
    """Run *func* in a thread; return (finished in time, its result or exception).

    The Library errors these tests expect (a listing still running, a refused
    registration) are handed back for the test to assert on; anything else
    stays an error in the thread.
    """
    import threading

    from services.libraries import LibraryPathError
    from services.rules import LibraryUnreadable

    box: dict = {}

    def run():
        try:
            box["value"] = func()
        except (LibraryUnreadable, LibraryPathError) as exc:
            box["value"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    return not worker.is_alive(), box.get("value")


@pytest.fixture(name="hung_walk")
def _hung_walk(monkeypatch):
    """Make every walk block until released, like a mount that stopped answering."""
    import threading

    from services.libraries.folder import FolderLibrary, Scan

    started = threading.Event()
    release = threading.Event()

    def hung(self):
        started.set()
        release.wait(30)
        return Scan()

    monkeypatch.setattr(FolderLibrary, "_walk", hung)
    yield started
    release.set()


@pytest.mark.asyncio
async def test_metadata_edits_and_lookups_do_not_wait_for_a_walk(
    tree: Path, hung_walk,
) -> None:
    import threading

    from services import libraries

    lib = await _register(tree / "ps2")
    library = libraries.get(lib["id"])
    threading.Thread(target=lambda: library.scan(refresh=True), daemon=True).start()
    assert hung_walk.wait(2)

    done, renamed = _finishes(lambda: libraries.update(lib["id"], name="PS2 games"))
    assert done, "update() waited for the walk"
    assert renamed.name == "PS2 games"
    done, _ = _finishes(lambda: libraries.update(lib["id"], schedule_enabled=True))
    assert done
    assert libraries.get(lib["id"]).schedule_enabled() is True
    done, found = _finishes(lambda: libraries.get(lib["id"]))
    assert done and found is not None
    done, other = _finishes(lambda: libraries.register(str(tree / "gc")))
    assert done, "register() waited for the walk"
    assert other.root.endswith("gc")


@pytest.mark.asyncio
async def test_a_hung_romm_root_does_not_block_lookups(db: Path, monkeypatch) -> None:
    import threading

    from services import libraries

    release = threading.Event()

    def hung_root():
        release.wait(30)

    monkeypatch.setattr(libraries, "_romm_root", hung_root)
    (db / "roms").mkdir()
    blocked = threading.Thread(
        target=lambda: _finishes(lambda: libraries.register(str(db / "roms")), 60),
        daemon=True,
    )
    blocked.start()
    try:
        done, found = _finishes(libraries.all_libraries)
        assert done, "a registration waiting on RomM's root blocked the catalogue"
        assert [lib.id for lib in found] == ["romm"]
    finally:
        release.set()
        blocked.join(5)


@pytest.mark.asyncio
async def test_a_second_walk_fails_fast_while_one_is_stuck(tree: Path, hung_walk) -> None:
    import threading

    from services import libraries
    from services.rules import LibraryUnreadable

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    threading.Thread(target=lambda: library.scan(refresh=True), daemon=True).start()
    assert hung_walk.wait(2)

    done, outcome = _finishes(lambda: library.images("ps2"), 1.0)
    assert done, "a second listing waited for the stuck walk"
    assert isinstance(outcome, LibraryUnreadable)
    assert "still running" in str(outcome)


@pytest.mark.asyncio
async def test_repeated_sweeps_on_a_hung_mount_strand_one_thread(
    tree: Path, ready, queue, hung_walk, monkeypatch,
) -> None:
    import asyncio
    import threading

    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    from services import notifications

    lib = await _library_with_rules(tree)
    monkeypatch.setattr(rules_sweep, "_LISTING_SECONDS", 0.2)
    # The sweep's error notification reads its destinations on a pooled worker,
    # which can start one more idle pool thread; not a stranded one.
    monkeypatch.setattr(notifications, "notify", lambda event: None)
    before = threading.active_count()

    for _ in range(6):
        result = await routes.run_library(lib["id"])
        assert result["queued"] == 0
        assert not rules_sweep._sweep_lock.locked()
    await asyncio.sleep(0.2)

    assert threading.active_count() - before <= 1
    # The shared pool still serves conversions.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 2) == 1


@pytest.mark.asyncio
async def test_preview_reports_held_images(tree: Path, ready, queue, monkeypatch) -> None:
    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    await routes.run_library(lib["id"])
    ico = (tree / "ps2" / "Ico.iso").resolve()
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Ico.iso"], status=JobStatus.FAILED, file_path=str(ico),
    ))
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)

    preview = await routes.preview_library(lib["id"])
    assert preview["skipped_failed"] == 1
    assert preview["platforms"][0]["skipped_failed"] == 1

    # Forget history releases it.
    await routes.forget_library_converted(lib["id"], {"platform_ids": ["ps2"]})
    assert (await routes.preview_library(lib["id"]))["queued"] == 1


@pytest.mark.asyncio
async def test_a_hold_clears_when_a_track_changes(psx: Path, ready, queue, monkeypatch) -> None:
    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    lib = await _register(psx)
    await routes.put_library_rules(lib["id"], {"rules": {
        "psx": {"mode": "createcd", "enabled": True},
    }})
    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    await routes.run_library(lib["id"])
    cue = (psx / "psx" / "Multi" / "Multi.cue").resolve()
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Multi.cue"], status=JobStatus.FAILED, file_path=str(cue),
    ))
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)
    queue.clear()
    held = await routes.run_library(lib["id"])
    assert "Multi.cue" not in [Path(p).name for p in queue], held

    # A bad track is replaced; the sheet itself is untouched.
    with (psx / "psx" / "Multi" / "Multi (Track 2).bin").open("ab") as fh:
        fh.write(b"\0" * 2048)
    queue.clear()
    await routes.run_library(lib["id"])
    assert "Multi.cue" in [Path(p).name for p in queue]


@pytest.mark.asyncio
async def test_holds_are_pruned_when_the_image_goes(tree: Path, ready, queue, monkeypatch) -> None:
    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    await routes.run_library(lib["id"])
    ico = tree / "ps2" / "Ico.iso"
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Ico.iso"], status=JobStatus.FAILED, file_path=str(ico.resolve()),
    ))
    state = (await routes.get_library_rules(lib["id"]))["state"]
    assert list(state["ps2"]["failed"]) == ["ps2/Ico.iso"]

    ico.unlink()
    await routes.get_library_rules(lib["id"])  # nothing pruned by a read
    from services import libraries

    libraries.get(lib["id"]).scan(refresh=True)
    await routes.run_library(lib["id"])
    state = (await routes.get_library_rules(lib["id"]))["state"]
    assert state["ps2"].get("failed", {}) == {}


@pytest.mark.asyncio
async def test_a_mount_blip_fails_one_sweep_and_the_next_recovers(
    db: Path, ready, queue, monkeypatch,
) -> None:
    """The reviewer's probe (round 3, R1): configured() never latches.

    The Volume's mount point vanishes for one walk (stale handle, autofs, a NAS
    reboot). That sweep reports why; the next one, with the Volume back,
    queues again without anyone opening the Rules view.
    """
    from routes import libraries as routes
    from services import libraries
    from services.libraries import folder

    monkeypatch.setattr(folder, "SCAN_TTL_SECONDS", 0)
    (db / "roms" / "ps2").mkdir(parents=True)
    (db / "roms" / "ps2" / "a.iso").write_bytes(b"\0" * 64)
    lib = await _register(db / "roms")
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    library = libraries.get(lib["id"])

    healthy = await routes.preview_library(lib["id"])
    assert healthy["queued"] == 1, healthy

    away = db.parent / "vol_away"
    os.rename(db, away)
    try:
        blip = await routes.preview_library(lib["id"])
    finally:
        os.rename(away, db)
    assert blip["queued"] == 0
    assert blip["stopped_reason"] is None
    assert blip["errors"] == [{"platform_id": "ps2", "error": "root_unavailable"}], blip
    assert library.configured() is True

    for _ in range(2):
        again = await routes.preview_library(lib["id"])
        assert again["queued"] == 1, again
        assert again["errors"] == []


@pytest.mark.asyncio
async def test_a_listing_in_progress_is_a_wait_in_preview_and_run(
    tree: Path, ready, queue, hung_walk,
) -> None:
    """Round 3, R3: the same neutral wait as the platforms route, not "could not list"."""
    import threading

    from routes import libraries as routes
    from services import libraries

    lib = await _library_with_rules(tree)
    library = libraries.get(lib["id"])
    threading.Thread(target=lambda: library.scan(refresh=True), daemon=True).start()
    assert hung_walk.wait(2)

    for call in (routes.preview_library, routes.run_library):
        result = await call(lib["id"])
        assert {e["error"] for e in result["errors"]} == {"listing_in_progress"}, result
        assert result["queued"] == 0


def test_concurrent_updates_leave_memory_and_the_database_agreeing(tree: Path, monkeypatch) -> None:
    """Round 3, R2: two PATCHes of one field end with the same value in both places."""
    import threading

    from services import db as db_module
    from services import libraries
    from services.libraries.folder import FolderLibrary

    library = libraries.register(str(tree))
    first_committed = threading.Event()
    real_apply = FolderLibrary.apply_settings
    calls = {"n": 0}

    def slow_first_apply(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            first_committed.set()
            time.sleep(0.4)
        return real_apply(self, **kwargs)

    monkeypatch.setattr(FolderLibrary, "apply_settings", slow_first_apply)
    first = threading.Thread(target=lambda: libraries.update(library.id, name="A"))
    first.start()
    assert first_committed.wait(2)
    second = threading.Thread(target=lambda: libraries.update(library.id, name="B"))
    second.start()
    first.join(5)
    second.join(5)

    with db_module.SessionLocal() as session:
        stored = session.get(db_module.FolderLibraryRecord, library.record_id).name
    assert libraries.get(library.id).name == stored


@pytest.mark.asyncio
async def test_registering_against_a_hung_romm_mount_is_bounded(
    db: Path, monkeypatch,
) -> None:
    """Round 3, R4: a dedicated thread under a bound, never a shared worker."""
    import asyncio
    import threading

    from routes import libraries as routes
    from services import libraries

    release = threading.Event()

    def hung_root():
        release.wait(30)

    monkeypatch.setattr(libraries, "_romm_root", hung_root)
    monkeypatch.setattr(routes, "_REGISTER_SECONDS", 0.3)
    monkeypatch.setattr(libraries, "REGISTER_WAIT_SECONDS", 0.2)
    (db / "a").mkdir()
    (db / "b").mkdir()
    try:
        with pytest.raises(HTTPException) as hung:
            await _register(db / "a")
        assert hung.value.status_code == 504
        started = time.monotonic()
        with pytest.raises(HTTPException) as busy:
            await _register(db / "b")
        assert busy.value.status_code in (409, 504)
        assert time.monotonic() - started < 2
        # Nothing waits in the shared pool.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 2) == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_hold_survives_a_restart_and_a_real_run(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """Round 3, R5: a still-listed hold is kept by a real Run, across a restart."""
    from models import JobStatus
    from routes import libraries as routes
    from services import libraries
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    await routes.run_library(lib["id"])
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Ico.iso"], status=JobStatus.FAILED,
        file_path=str((tree / "ps2" / "Ico.iso").resolve()),
    ))
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)

    # A restart: the in-memory catalogue and job owners are gone; the state row is not.
    libraries.reset_for_tests()
    monkeypatch.setattr(rules_sweep, "_job_owners", {})

    for _ in range(2):
        result = await routes.run_library(lib["id"])
        assert result["skipped_failed"] == 1, result
        assert result["queued"] == 0
    state = (await routes.get_library_rules(lib["id"]))["state"]
    assert list(state["ps2"]["failed"]) == ["ps2/Ico.iso"]


def test_a_changed_track_re_detects_its_sheet(db: Path, monkeypatch) -> None:
    """The Library's detection cache keys a sheet on its tracks too (#294's header_reads)."""
    from services.libraries.folder import FolderLibrary

    roms = db / "roms"
    _psx_tree(roms)
    library = FolderLibrary(record_id=1, root=str(roms.resolve()), name="r")
    decided: list[str] = []
    real = platform_detect._decide

    def counting(path, *args, **kwargs):
        decided.append(Path(path).name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(platform_detect, "_decide", counting)
    library.summary(refresh=True)
    decided.clear()

    with (roms / "psx" / "Multi" / "Multi (Track 2).bin").open("ab") as fh:
        fh.write(b"\0" * 2048)
    library.summary(refresh=True)

    assert decided == ["Multi.cue"]


@pytest.mark.asyncio
async def test_a_listing_still_running_is_its_own_409(tree: Path, hung_walk) -> None:
    """Not "unreadable": the Rules view shows it as a neutral wait with a retry."""
    import threading

    from routes import libraries as routes
    from services import libraries

    lib = await _register(tree)
    library = libraries.get(lib["id"])
    threading.Thread(target=lambda: library.scan(refresh=True), daemon=True).start()
    assert hung_walk.wait(2)

    with pytest.raises(HTTPException) as exc:
        await routes.library_platforms(lib["id"])
    assert exc.value.status_code == 409
    assert exc.value.detail == routes.LISTING_IN_PROGRESS


# ----------------------------------------------------------------------
# before the PR (review round 4 minors)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gone_folder_is_logged_once_and_its_recovery_once(
    db: Path, ready, queue, monkeypatch, caplog,
) -> None:
    """M2: one warning when it becomes unreadable, one line when it recovers."""
    import logging

    from routes import libraries as routes
    from services.libraries import folder

    monkeypatch.setattr(folder, "SCAN_TTL_SECONDS", 0)
    (db / "roms" / "ps2").mkdir(parents=True)
    (db / "roms" / "ps2" / "a.iso").write_bytes(b"\0" * 64)
    lib = await _register(db / "roms")
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    await routes.preview_library(lib["id"])
    caplog.set_level(logging.INFO)

    away = db.parent / "vol_away"
    os.rename(db, away)
    try:
        for _ in range(3):
            await routes.preview_library(lib["id"])
    finally:
        os.rename(away, db)
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "unreadable" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]

    for _ in range(3):
        await routes.preview_library(lib["id"])
    recovered = [r for r in caplog.records if "readable again" in r.getMessage()]
    assert len(recovered) == 1


@pytest.mark.asyncio
async def test_registering_a_folder_twice_says_the_first_registration_finished(
    tree: Path,
) -> None:
    """M1: after a 504 the first registration may still have finished; a retry says so."""
    await _register(tree)

    with pytest.raises(HTTPException) as exc:
        await _register(tree)
    assert exc.value.status_code == 409
    assert "earlier registration of this folder has finished" in exc.value.detail


@pytest.mark.asyncio
async def test_a_library_no_longer_reports_a_constant_configured(tree: Path) -> None:
    """M3: `configured` was always True, so the API no longer carries it."""
    from routes import libraries as routes

    lib = await _register(tree)
    assert "configured" not in lib
    assert all("configured" not in entry for entry in await routes.list_libraries())


# ----------------------------------------------------------------------
# PR #320 review
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_images_size_is_all_of_its_files(psx: Path, ready, queue) -> None:
    """Codex 4576551531: a 67-byte .cue for a large .bin is the size of the set."""
    from routes import libraries as routes
    from services import libraries

    lib = await _register(psx)
    images = {i.key: i for i in libraries.get(lib["id"]).images("psx")}
    multi = psx / "psx" / "Multi"
    expected = sum(
        (multi / name).stat().st_size
        for name in ("Multi.cue", "Multi (Track 1).bin", "Multi (Track 2).bin")
    )
    assert images["psx/Multi/Multi.cue"].size == expected

    # A size filter judges the whole set: 8 KiB of tracks pass a 6 KiB minimum.
    await routes.put_library_rules(lib["id"], {"rules": {
        "psx": {"mode": "createcd", "enabled": True, "min_size_mb": 0},
    }})
    for path in (multi / "Multi (Track 1).bin", multi / "Multi (Track 2).bin"):
        path.write_bytes(b"\0" * (1024 * 1024))
    libraries.get(lib["id"]).scan(refresh=True)
    await routes.put_library_rules(lib["id"], {"rules": {
        "psx": {"mode": "createcd", "enabled": True, "min_size_mb": 1},
    }})
    await routes.run_library(lib["id"])
    assert [Path(p).name for p in queue] == ["Multi.cue"]


@pytest.mark.asyncio
async def test_removing_a_library_mid_callback_leaves_no_holds(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """Codex 4576551534: a failure noted while the Library is removed writes nothing."""
    import asyncio

    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {"ps2": _RULES["ps2"]}})
    await routes.run_library(lib["id"])

    probing = asyncio.Event()
    release = asyncio.Event()
    real_probe = rules_sweep._probe

    async def slow_probe(func, *args, default):
        probing.set()
        await release.wait()
        return await real_probe(func, *args, default=default)

    monkeypatch.setattr(rules_sweep, "_probe", slow_probe)
    noting = asyncio.create_task(rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Ico.iso"], status=JobStatus.FAILED,
        file_path=str((tree / "ps2" / "Ico.iso").resolve()),
    )))
    await asyncio.wait_for(probing.wait(), 2)
    await routes.remove_library(lib["id"])
    release.set()
    await noting

    blob = await preferences_store.get("rules.state") or {}
    assert not any(key.startswith(f"{lib['id']}:") for key in blob), blob


@pytest.mark.asyncio
async def test_a_removed_librarys_id_is_never_reused(tree: Path) -> None:
    """Codex 4576551534: state keyed by Library id must not reach a new Library."""
    from routes import libraries as routes

    first = await _register(tree / "gc")
    await routes.remove_library(first["id"])
    second = await _register(tree / "ps2")
    assert second["id"] != first["id"]


@pytest.mark.asyncio
async def test_a_sheet_that_escapes_the_volume_is_never_queued(
    db: Path, ready, queue,
) -> None:
    """Codex security P1 (PR #320): chdman opens a sheet's tracks itself.

    A planted ``.cue`` naming a file outside the Volume (by ``..``, by an
    absolute path, or by a symlinked track) must never reach the queue, and
    the sweep says why. A normal sheet beside it still converts.
    """
    from routes import libraries as routes

    secret = db.parent / "compressatorium.secret"
    secret.write_bytes(b"secret")
    psx = db / "roms" / "psx"
    psx.mkdir(parents=True)
    (psx / "Escape.cue").write_text('FILE "../../../compressatorium.secret" BINARY\n')
    (psx / "Absolute.cue").write_text(f'FILE "{secret}" BINARY\n')
    (psx / "Linked.bin").symlink_to(secret)
    (psx / "Linked.cue").write_text('FILE "Linked.bin" BINARY\n')
    (psx / "Good.bin").write_bytes(b"\0" * 4096)
    (psx / "Good.cue").write_text('FILE "Good.bin" BINARY\n')

    lib = await _register(db / "roms")
    await routes.put_library_rules(lib["id"], {"rules": {
        "psx": {"mode": "createcd", "enabled": True},
    }})
    preview = await routes.preview_library(lib["id"])
    result = await routes.run_library(lib["id"])

    assert [Path(p).name for p in queue] == ["Good.cue"], result
    for summary in (preview, result):
        assert summary["skipped_unsafe"] == 3, summary
        platform = summary["platforms"][0]
        assert sorted(platform["unsafe"]) == ["Absolute.cue", "Escape.cue", "Linked.cue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", [
    "{vol}/../outside",          # .. out of the Volume
    "{vol}/roms/../../outside",  # .. deeper in
    "../../outside",             # relative
    "{link}",                    # a symlink inside the Volume pointing out
])
async def test_a_rule_output_folder_cannot_leave_the_volumes(
    tree: Path, tmp_path: Path, escape: str,
) -> None:
    """CodeQL py/path-injection (PR #320): the output folder a Rule names is confined.

    PUT /api/libraries/{id}/rules carries a user-chosen output folder into the
    Volume check. A path that leaves the Volumes, by ``..`` or by a symlink,
    is refused: the Rule is paused with the folder recorded, never written to.
    """
    from routes import libraries as routes

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tree / "escape-link"
    link.symlink_to(outside)
    output_dir = escape.format(vol=tree.parent, link=link)

    lib = await _register(tree)
    saved = await routes.put_library_rules(lib["id"], {"rules": {
        "ps2": {"mode": "createdvd", "enabled": True, "output_dir": output_dir},
    }})

    rule = saved["rules"]["ps2"]
    assert rule["output_dir"] is None
    assert rule["invalid_output_dir"] == output_dir
    assert rule["enabled"] is False


def test_the_volume_check_confines_a_path_before_touching_it(tree: Path, tmp_path: Path) -> None:
    """The containment answers the Volume check must keep (pinned for the CodeQL fix in #294)."""
    from utils.path_utils import is_within_configured_volumes

    vol = tree.parent
    assert is_within_configured_volumes(str(vol)) is True
    assert is_within_configured_volumes(str(vol / "roms" / "new-folder")) is True
    assert is_within_configured_volumes(str(vol / ".." / "x")) is False
    # A sibling whose name merely starts with the Volume's name is not inside it.
    assert is_within_configured_volumes(str(vol) + "-sibling") is False
    loop = vol / "loop"
    loop.symlink_to(loop)
    assert is_within_configured_volumes(str(loop)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("file_line", [
    'FILE\t"game.bin" BINARY',  # a tab after FILE
    "FILE game.bin BINARY",     # an unquoted name
])
async def test_grouping_and_detection_read_a_sheet_the_same_way(
    db: Path, file_line: str,
) -> None:
    """Codex 4576629009: one CUE parser (utils.sheet_text) for grouping and detection.

    No Platform-named folder, so only the header can name the Platform: the
    sheet's track is a Saturn disc. The .bin is hidden behind its sheet, and
    the sheet is detected as Saturn, so the Image is not lost to Unsorted.
    """
    from routes import libraries as routes
    from services import libraries

    from .test_platform_detect import _sector0

    folder = db / "games" / "misc"
    folder.mkdir(parents=True)
    _sector0(folder / "game.bin", b"SEGA SEGASATURN ", raw=True)
    (folder / "game.cue").write_text(file_line + "\n  TRACK 01 MODE1/2352\n")

    lib = await _register(db / "games")
    summary = await routes.library_platforms(lib["id"])

    assert {p["id"]: p["images"] for p in summary["platforms"]} == {"saturn": 1}, summary
    assert summary["unsorted"] == 0
    keys = [i.key for i in libraries.get(lib["id"]).images("saturn")]
    assert keys == ["misc/game.cue"]


def test_no_other_cue_file_line_parser_remains() -> None:
    """Codex 4576629009: utils.sheet_text.cue_tracks is the only FILE-line reader."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path.name != "sheet_text.py"
        and 'startswith("FILE")' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_a_failure_under_an_old_rule_holds_nothing_under_the_new_one(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """Codex 4576629016: queue RVZ, retarget to WIA, fail the RVZ job.

    The hold belongs to the Rule that queued the job. After the Rule is
    retargeted, the old job's failure is dropped, and the WIA preview queues
    the Image.
    """
    from models import JobStatus
    from routes import libraries as routes
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {
        "ngc": {"mode": "dolphin_rvz", "enabled": True},
    }})
    await routes.run_library(lib["id"])

    await routes.put_library_rules(lib["id"], {"rules": {
        "ngc": {"mode": "dolphin_wia", "enabled": True},
    }})
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Melee.iso"], status=JobStatus.FAILED,
        file_path=str((tree / "gc" / "Melee.iso").resolve()),
    ))
    monkeypatch.setattr(job_manager, "get_active_job_candidates", list)

    preview = await routes.preview_library(lib["id"])
    assert preview["skipped_failed"] == 0, preview
    assert "Melee.iso" in preview["platforms"][0]["candidates"]


@pytest.mark.asyncio
async def test_a_split_part_added_later_joins_its_image(db: Path) -> None:
    """Codex 4576709447: a cached membership follows the folder's current parts.

    Scan with only part 1, add part 2, refresh: one Image of 30 bytes, not two.
    """
    from services import libraries

    folder = db / "roms" / "wiiu"
    folder.mkdir(parents=True)
    (folder / "game_part1.wud").write_bytes(b"\0" * 10)
    lib = await _register(db / "roms")
    library = libraries.get(lib["id"])
    library.scan(refresh=True)

    (folder / "game_part2.wud").write_bytes(b"\0" * 20)
    library.scan(refresh=True)

    images = library.images("wiiu")
    assert [(i.key, i.size) for i in images] == [("wiiu/game_part1.wud", 30)]


@pytest.mark.asyncio
async def test_a_walk_builds_each_folders_listing_marker_once(db: Path, monkeypatch) -> None:
    """Codex 4576751001: the membership cache key holds a per-folder marker.

    Each folder's listing is turned into one generation marker per walk, and
    every file in it shares that marker, so a cached walk compares a short
    value per file instead of the whole folder's names (quadratic before).
    """
    from services import libraries
    from services.libraries import folder as folder_mod

    roms = db / "roms"
    for name, count in (("a", 40), ("b", 25)):
        (roms / name).mkdir(parents=True)
        for i in range(count):
            (roms / name / f"g{i}.iso").write_bytes(b"\0" * 16)
    lib = await _register(roms)
    library = libraries.get(lib["id"])

    built: list[int] = []
    real = folder_mod._folder_generation

    def counting(names) -> str:
        built.append(len(names))
        return real(names)

    monkeypatch.setattr(folder_mod, "_folder_generation", counting)
    for _ in range(2):  # a cold walk, then a cached one
        built.clear()
        library.scan(refresh=True)
        assert sorted(built) == [25, 40]  # one listing per folder per walk

    for key, _value in library._members.values():
        assert all(not isinstance(part, (set, frozenset)) for part in key)


@pytest.mark.asyncio
async def test_a_backslash_track_path_is_one_detected_image(db: Path) -> None:
    """Codex 4576790199: grouping and detection read ``tracks\\game.bin`` alike.

    As chdman does on POSIX (#294's 2b246fe), the backslash is part of the
    name: the sheet names the file ``tracks\\game.bin`` beside it. No folder
    above the sheet names a Platform, so only the header can: that Saturn
    track must be found by detection (the probe) and owned by the sheet
    (grouping), giving one Saturn Image and nothing else.
    """
    from services import libraries

    from .test_platform_detect import _sector0

    root = db / "collection"
    game = root / "Game"
    game.mkdir(parents=True)
    (game / "game.cue").write_text(
        'FILE "tracks\\game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n',
    )
    _sector0(game / "tracks\\game.bin", b"SEGA SEGASATURN ", raw=True)
    lib = await _register(root)
    library = libraries.get(lib["id"])

    scan = library.scan(refresh=True)
    assert [e.key for e in scan.entries] == ["Game/game.cue"]
    assert scan.entries[0].detection.platform == "saturn"
    assert [i.key for i in library.images("saturn")] == ["Game/game.cue"]


async def _fail_melee_while_probing(tree: Path, monkeypatch, during) -> dict:
    """Queue Melee.iso under RVZ, then fail it; *during* runs inside the hold's
    fingerprint probe, before the hold is written. Returns the ngc state."""
    from models import JobStatus
    from routes import libraries as routes
    from services.image_sets import image_fingerprint
    from services.rules import sweep as rules_sweep

    jobs: dict[str, str] = {}
    real_batch = job_manager.create_batch_jobs

    async def batch(paths, mode, **kwargs):
        made = await real_batch(paths, mode, **kwargs)
        jobs.update({Path(j.file_path).name: j.id for j in made})
        return made

    monkeypatch.setattr(job_manager, "create_batch_jobs", batch)
    (tree / "gc" / "Locked.iso").unlink()
    lib = await _register(tree)
    await routes.put_library_rules(lib["id"], {"rules": {
        "ngc": {"mode": "dolphin_rvz", "enabled": True},
    }})
    await routes.run_library(lib["id"])

    real_probe = rules_sweep._probe

    async def probe(fn, *args, **kwargs):
        if fn is image_fingerprint:
            await during(lib)
        return await real_probe(fn, *args, **kwargs)

    monkeypatch.setattr(rules_sweep, "_probe", probe)
    await rules_sweep.note_job_finished(SimpleNamespace(
        id=jobs["Melee.iso"], status=JobStatus.FAILED,
        file_path=str((tree / "gc" / "Melee.iso").resolve()),
    ))
    return (await routes.get_library_rules(lib["id"]))["state"].get("ngc") or {}


@pytest.mark.asyncio
async def test_a_rule_retargeted_during_the_hold_probe_gets_no_hold(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """CodeRabbit 4576895547: the Rule identity is checked again under the lock.

    The RVZ job fails; while its hold's fingerprint is being read the Rule is
    retargeted to WIA. The RVZ failure must not be held under the WIA Rule.
    """
    from routes import libraries as routes

    async def retarget(lib):
        await routes.put_library_rules(lib["id"], {"rules": {
            "ngc": {"mode": "dolphin_wia", "enabled": True},
        }})

    state = await _fail_melee_while_probing(tree, monkeypatch, retarget)
    assert not state.get("failed"), state


@pytest.mark.asyncio
async def test_a_source_repaired_before_the_hold_probe_gets_no_hold(
    tree: Path, ready, queue, monkeypatch,
) -> None:
    """CodeRabbit 4576895550: the hold is bound to the Source the job had.

    The job fails; the operator replaces the Source before the hold's
    fingerprint is read. The repaired file never failed, so it is not held.
    """
    melee = tree / "gc" / "Melee.iso"

    async def repair(_lib):
        melee.write_bytes(melee.read_bytes() + b"\0" * 4096)

    state = await _fail_melee_while_probing(tree, monkeypatch, repair)
    assert not state.get("failed"), state


@pytest.mark.asyncio
async def test_an_unchanged_source_is_still_held(tree: Path, ready, queue, monkeypatch) -> None:
    """The control: nothing changes during the probe, so the failure is held."""
    async def nothing(_lib):
        return None

    state = await _fail_melee_while_probing(tree, monkeypatch, nothing)
    assert list(state.get("failed") or {}) == ["gc/Melee.iso"], state


def test_the_folder_libraries_migration_stacks_on_the_ledger_totals_one(tmp_path: Path) -> None:
    """Supervisor ruling: #297 keeps 0011; #303's table comes in 0012 on top.

    One linear head. Migrations here are forward-only (every ``downgrade``
    raises), so the chain is walked upward instead: an empty database taken
    to 0010, then 0011, then head, each table arriving with its own revision,
    and a straight upgrade of another empty database to head.
    """
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    engine = _db.make_engine(str(tmp_path / "chain.db"))
    cfg = _db._alembic_config(engine)
    script = ScriptDirectory.from_config(cfg)
    # #304's 0013 stacks on this 0012, #305's 0014 on that, #337's 0015 on
    # that, #386's 0016 on that, #388's 0017 on that, #387's 0018 on that
    # #389's 0019 on that, #391's 0020 on that, #392's 0021 on that, #424's
    # 0022 on that, #430's 0023 on that, #423's 0024 on that and #431's 0025
    # on that: one head, 0025.
    assert script.get_heads() == ["0025"]
    assert script.get_revision("0012").down_revision == "0011"
    assert script.get_revision("0011").down_revision == "0010"

    def tables(eng) -> set[str]:
        return set(inspect(eng).get_table_names())

    command.upgrade(cfg, "0010")
    assert not {"folder_libraries", "ledger_totals"} & tables(engine)
    command.upgrade(cfg, "0011")
    assert "ledger_totals" in tables(engine)
    assert "folder_libraries" not in tables(engine)
    command.upgrade(cfg, "head")
    assert {"folder_libraries", "ledger_totals"} <= tables(engine)

    fresh = _db.make_engine(str(tmp_path / "fresh.db"))
    command.upgrade(_db._alembic_config(fresh), "head")
    assert {"folder_libraries", "ledger_totals"} <= tables(fresh)


@pytest.mark.asyncio
async def test_an_ancestor_swapped_for_a_symlink_re_detects(db: Path) -> None:
    """CodeRabbit 4578050375: the walk's folder context is part of the cache key.

    ``ps2`` is cached for ``ps2/library/game.iso`` from its folder. Rename
    ``ps2`` to ``neutral`` and put a ``ps2`` symlink to it in its place: the
    Library path and the file's stat are unchanged, but the folder the file
    really sits in no longer names a Platform, so the next walk re-detects.
    """
    import os as _os

    from services import libraries

    library_dir = db / "ps2" / "library"
    library_dir.mkdir(parents=True)
    _blank(library_dir / "game.iso")
    lib = await _register(library_dir)
    library = libraries.get(lib["id"])

    first = library.scan(refresh=True)
    assert [e.detection.platform for e in first.entries] == ["ps2"]

    _os.rename(db / "ps2", db / "neutral")
    (db / "ps2").symlink_to(db / "neutral", target_is_directory=True)

    second = library.scan(refresh=True)
    assert [e.detection.platform for e in second.entries] == [None]


def test_the_detection_key_names_its_probe_set(db: Path) -> None:
    """CodeRabbit 4576895544: ``detection_key`` itself carries the probe set.

    Both caches key on it (the shared one and a folder Library's), so a
    Detection made by one set of tools is never reused for another.
    """
    from services.tools import registry

    iso = db / "neutral" / "game.iso"
    iso.parent.mkdir(parents=True)
    _gamecube(iso)
    st = os.stat(iso)
    chdman = [registry.get("chdman")]
    dolphin = [registry.get("dolphin")]

    assert platform_detect.detection_key(str(iso), st, tools=chdman) == (
        platform_detect.detection_key(str(iso), st, tools=list(chdman))
    )
    assert platform_detect.detection_key(str(iso), st, tools=chdman) != (
        platform_detect.detection_key(str(iso), st, tools=dolphin)
    )


@pytest.mark.asyncio
async def test_a_slow_walk_is_reused_from_when_it_finished(db: Path, monkeypatch) -> None:
    """CodeRabbit 4576895543: the reuse window starts when the walk is ready.

    Detection here takes 40 s of (fake) clock, longer than the 30 s window;
    the next listing right after must reuse the walk, not repeat it.
    """
    from types import SimpleNamespace

    from services import libraries
    from services.libraries import folder as folder_mod

    (db / "roms" / "ps2").mkdir(parents=True)
    _ps2(db / "roms" / "ps2" / "Game.iso")
    lib = await _register(db / "roms")
    library = libraries.get(lib["id"])

    clock = {"now": 1000.0}
    monkeypatch.setattr(folder_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    real_detect = folder_mod.platform_detect.detect_uncached

    def slow_detect(path, **kwargs):
        clock["now"] += 40.0
        return real_detect(path, **kwargs)

    monkeypatch.setattr(folder_mod.platform_detect, "detect_uncached", slow_detect)
    walks: list[int] = []
    real_files = library._files

    def counting_files():
        walks.append(1)
        return real_files()

    monkeypatch.setattr(library, "_files", counting_files)

    library.scan(refresh=True)
    library.scan()
    assert len(walks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["same", "parent", "child"])
async def test_a_romm_root_overlapping_a_folder_library_is_refused(
    db: Path, where: str,
) -> None:
    """Codex 4576816771: saving RomM's library root checks every folder Library.

    The same overlap rule as registration, the other way round: a RomM root
    equal to, containing, or inside a registered folder is a 409 naming the
    Library, and nothing is saved.
    """
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings

    folder = db / "games" / "gc"
    (folder / "sub").mkdir(parents=True)
    await _register(folder, name="GameCube")
    root = {"same": folder, "parent": db / "games", "child": folder / "sub"}[where]

    with pytest.raises(HTTPException) as exc:
        await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
            url="http://romm:8080", library_root=str(root),
        ))
    assert exc.value.status_code == 409
    assert "GameCube" in exc.value.detail
    assert not romm_settings.effective().get("library_root")


@pytest.mark.asyncio
async def test_a_hung_romm_root_check_answers_503_and_holds_no_lock(
    db: Path, monkeypatch,
) -> None:
    """CodeRabbit 4576895541: the RomM root check is bounded, and runs first.

    The check runs before the settle lock and the sweep pause are taken. Past
    its bound the save is refused with 503, nothing is saved, and every lock
    is free again.
    """
    import threading

    from routes import romm as romm_routes
    from services import libraries
    from services.romm import settings as romm_settings

    (db / "romm").mkdir()
    release = threading.Event()
    seen: dict[str, bool] = {}

    def hung_check(path):
        seen["settle_locked"] = romm_routes._settle_lock.locked()
        release.wait(10)

    monkeypatch.setattr(libraries, "check_romm_root", hung_check)
    monkeypatch.setattr(libraries, "ROOT_CHECK_SECONDS", 0.2)
    try:
        with pytest.raises(HTTPException) as exc:
            await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
                url="http://romm:8080", library_root=str(db / "romm"),
            ))
        assert exc.value.status_code == 503
        assert seen == {"settle_locked": False}
        assert not romm_routes._settle_lock.locked()
        assert not libraries._register_lock.locked()
        assert not romm_settings.effective().get("library_root")
    finally:
        release.set()


@pytest.mark.asyncio
async def test_registration_workers_are_capped_until_they_finish(
    db: Path, monkeypatch,
) -> None:
    """CodeRabbit 4576895542: admission control for registration workers.

    Two registrations stuck on a dead mount time out but keep their slots
    until their workers actually return; a third is refused with 503 at once,
    without starting a worker. Once the mount answers, registering works.
    """
    import asyncio
    import threading

    from routes import libraries as routes
    from services import libraries

    release = threading.Event()
    entered: list[str] = []
    real_check = libraries.ensure_path_within_volumes

    def hung_check(path, **kwargs):
        entered.append(path)
        release.wait(10)
        return real_check(path, **kwargs)

    monkeypatch.setattr(libraries, "ensure_path_within_volumes", hung_check)
    monkeypatch.setattr(routes, "_REGISTER_SECONDS", 0.2)
    monkeypatch.setattr(libraries, "_worker_slots", threading.BoundedSemaphore(2))
    folders = []
    for name in ("a", "b", "c", "d"):
        (db / name).mkdir()
        folders.append(db / name)
    try:
        for folder in folders[:2]:
            with pytest.raises(HTTPException) as timed_out:
                await _register(folder)
            assert timed_out.value.status_code == 504
        with pytest.raises(HTTPException) as full:
            await _register(folders[2])
        assert full.value.status_code == 503
        assert len(entered) == 2  # the third never started a worker
    finally:
        release.set()
    for _ in range(100):  # the two workers finish and give their slots back
        if libraries._worker_slots.acquire(blocking=False):
            libraries._worker_slots.release()
            if len(libraries.folder_libraries()) == 2:
                break
        await asyncio.sleep(0.05)
    created = await _register(folders[3])
    assert created["root"] == str(folders[3].resolve())


@pytest.mark.asyncio
async def test_a_romm_root_beside_a_folder_library_saves(db: Path) -> None:
    from routes import romm as romm_routes
    from services.romm import settings as romm_settings

    (db / "games" / "gc").mkdir(parents=True)
    (db / "romm").mkdir()
    await _register(db / "games" / "gc")

    await romm_routes.put_romm_settings(romm_routes.RommSettingsPatch(
        url="http://romm:8080", library_root=str(db / "romm"),
    ))
    assert romm_settings.effective()["library_root"] == str(db / "romm")


@pytest.mark.asyncio
async def test_registering_romms_own_root_is_refused(db: Path) -> None:
    """The opposite order: RomM first, then a folder Library at the same root."""
    from services.romm import settings as romm_settings

    library = db / "library"
    library.mkdir()
    await romm_settings.save({"url": "http://romm:8080", "library_root": str(library)})

    with pytest.raises(HTTPException) as exc:
        await _register(library)
    assert exc.value.status_code == 409
