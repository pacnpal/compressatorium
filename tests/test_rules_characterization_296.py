"""RomM Rules as the API sees them, pinned before the engine moved (#296).

Written against the code as it stood before the Rule engine left
``services/romm``, and kept unchanged after the move. Everything here goes
through the route functions, the RomM settings and the stored preference
rows, never through a module the move touched, so a pass on both sides of the
move means the API shapes, the stored data and the scheduler behave the same.
"""


from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from routes import romm as romm_routes
from routes import libraries as lib_routes
from services import db as _db
from services.job_manager import job_manager
from services.preferences_store import preferences_store

from config import settings as app_settings

# The keys RomM Rules were stored under before #296. Written as literals on
# purpose: a database from an older release holds exactly these strings.
OLD_RULES_KEY = "romm.rules"
OLD_STATE_KEY = "romm.auto_state"


class _Job:
    def __init__(self, file_path: str, job_id: str) -> None:
        self.file_path = file_path
        self.id = job_id


@pytest.fixture(name="romm_db")
def _romm_db(tmp_path: Path, monkeypatch):
    """A database, a configured RomM, and a library inside the Volume."""
    from services.romm import settings as romm_settings

    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    monkeypatch.setattr(app_settings, "chd_volumes", str(tmp_path))
    lib = tmp_path / "library" / "roms" / "gc"
    lib.mkdir(parents=True)
    (lib / "Game.iso").write_bytes(b"\0" * 32)
    (lib / "Other.iso").write_bytes(b"\0" * 64)
    yield lib
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest_asyncio.fixture(name="romm_env")
async def _romm_env(romm_db, tmp_path: Path):
    from services.romm import settings as romm_settings

    await romm_settings.save({
        "url": "http://romm:8080", "library_root": str(tmp_path / "library"),
    })
    return romm_db


ROMS = [
    {
        "id": 1, "name": "Game", "full_path": "roms/gc/Game.iso",
        "fs_name": "Game.iso", "fs_size_bytes": 32, "platform_slug": "ngc",
    },
    {
        "id": 2, "name": "Other", "full_path": "roms/gc/Other.iso",
        "fs_name": "Other.iso", "fs_size_bytes": 64, "platform_slug": "ngc",
        "igdb_id": 99,
    },
]

SWEEP_KEYS = {
    "queued", "considered", "repins_recorded", "skipped_existing",
    "skipped_active", "skipped_filtered", "skipped_unconvertible",
    "skipped_missing", "platforms", "errors", "stopped_reason", "dry_run",
    "pending_repins",
}


def _catalog():
    return patch.object(romm_routes.romm_client, "roms", return_value=ROMS)


def _idle_queue():
    return patch.object(job_manager, "get_active_job_candidates", return_value=[])


def _accepting_queue(queued: list):
    async def _create(paths, mode, **kwargs):
        queued.append((list(paths), kwargs))
        return [_Job(p, f"job-{i}") for i, p in enumerate(paths)]

    return patch.object(job_manager, "create_batch_jobs", _create)


@pytest.mark.asyncio
async def test_the_rules_api_keeps_its_shape(romm_env) -> None:
    saved = await lib_routes.put_library_rules("romm", {"rules": {
        "07": {"mode": "dolphin_rvz", "enabled": True, "max_per_run": 3},
        "not-a-platform": {"mode": "dolphin_rvz"},
        "9": {"mode": "not_a_real_mode"},
    }})
    # Platform ids are RomM's integers, written back as canonical strings;
    # anything else, and any rule naming an unknown mode, is dropped.
    assert list(saved["rules"]) == ["7"]
    assert saved["rules"]["7"]["max_per_run"] == 3

    got = await lib_routes.get_library_rules("romm")
    # #305 added the check Rules beside the conversion Rules; the rest is pinned.
    assert set(got) == {"rules", "state", "defaults", "options", "check_rules", "check_state"}
    assert got["rules"] == saved["rules"]
    assert got["state"] == {}
    assert set(got["defaults"]) == set(got["rules"]["7"])
    assert got["defaults"]["mode"] == ""
    assert got["defaults"]["interval_minutes"] == 60
    assert got["defaults"]["max_per_run"] == 25
    assert got["options"] == {
        "orders": ["name", "size_desc", "size_asc", "id", "newest"],
        "duplicate_actions": ["skip", "overwrite", "rename"],
        "days": [0, 1, 2, 3, 4, 5, 6],
    }


@pytest.mark.asyncio
async def test_rule_defaults_follow_the_romm_settings(romm_env) -> None:
    from services.romm import settings as romm_settings

    await romm_settings.save({
        "auto_convert_interval_minutes": 15, "auto_convert_max_per_run": 4,
        "verify_after_convert": True,
    })
    got = await lib_routes.get_library_rules("romm")
    assert got["defaults"]["interval_minutes"] == 15
    assert got["defaults"]["max_per_run"] == 4
    assert got["defaults"]["verify_after"] is True
    saved = await lib_routes.put_library_rules("romm", {"7": {"mode": "dolphin_rvz"}})
    assert saved["rules"]["7"]["interval_minutes"] == 15
    assert saved["rules"]["7"]["max_per_run"] == 4


@pytest.mark.asyncio
async def test_preview_and_run_keep_their_summary_shape(romm_env) -> None:
    await lib_routes.put_library_rules("romm", {"7": {
        "mode": "dolphin_rvz", "enabled": True, "order": "size_desc",
    }})
    queued: list = []
    with _catalog(), _idle_queue(), _accepting_queue(queued):
        preview = await lib_routes.preview_library("romm", 
            lib_routes.SweepRequest(platform_ids=["7"]),
        )
        assert queued == []
        assert (await lib_routes.get_library_rules("romm"))["state"] == {}

        run = await lib_routes.run_library("romm", 
            lib_routes.SweepRequest(platform_ids=["7"], limit=1),
        )

    assert set(preview) == SWEEP_KEYS
    assert preview["dry_run"] is True
    assert preview["queued"] == 2
    assert preview["platforms"] == [{
        "platform_id": 7, "mode": "dolphin_rvz", "queued": 2,
        "repins_recorded": 0, "considered": 2,
        # size_desc: the larger Image first.
        "candidates": ["Other.iso", "Game.iso"],
    }]

    assert set(run) == SWEEP_KEYS
    assert run["dry_run"] is False
    assert run["queued"] == 1
    assert run["stopped_reason"] is None
    assert [paths for paths, _ in queued] == [[str(romm_env / "Other.iso")]]
    summary = run["platforms"][0]
    assert summary["platform_id"] == 7
    assert summary["candidates"] == ["Other.iso"]
    # Other.iso carries an igdb id and RVZ is a format RomM cannot hash-match,
    # so its metadata was snapshotted for re-pin.
    assert summary["repins_recorded"] == 1
    assert run["repins_recorded"] == 1
    assert run["pending_repins"] == 1

    state = (await lib_routes.get_library_rules("romm"))["state"]
    assert list(state) == ["7"]
    assert state["7"]["last_queued"] == 1
    assert state["7"]["last_considered"] == 1
    assert state["7"]["last_run_at"].endswith("Z")


@pytest.mark.asyncio
async def test_converted_history_is_kept_by_platform_and_rom_id(romm_env) -> None:
    await lib_routes.put_library_rules("romm", {"7": {
        "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
    }})
    queued: list = []
    with _catalog(), _idle_queue(), _accepting_queue(queued):
        first = await lib_routes.run_library("romm", 
            lib_routes.SweepRequest(platform_ids=["7"]),
        )
    assert first["queued"] == 2

    state = (await lib_routes.get_library_rules("romm"))["state"]
    converted = state["7"]["converted"]
    assert set(converted) == {"1", "2"}
    assert converted["1"]["path"] == str(romm_env / "Game.rvz")
    assert {"path", "pre", "job_id"} <= set(converted["1"])

    # A job that ends is written down against its ROM.
    job_id = converted["1"]["job_id"]
    from models import JobStatus
    from services.romm import auto as romm_auto

    finished = _Job(converted["1"]["path"], job_id)
    finished.status = JobStatus.FAILED
    await romm_auto.note_job_finished(finished)
    state = (await lib_routes.get_library_rules("romm"))["state"]
    assert state["7"]["converted"]["1"]["done"] is False

    forgot = await lib_routes.forget_library_converted("romm", {"platform_ids": [7]})
    assert forgot["cleared"] == 1
    assert "converted" not in forgot["state"]["7"]
    assert "last_run_at" in forgot["state"]["7"]


@pytest.mark.asyncio
async def test_rules_stored_by_an_earlier_release_keep_working(romm_env) -> None:
    """A database written before the move is read exactly as it was."""
    rule = {"mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite"}
    await preferences_store.put(OLD_RULES_KEY, {"7": rule})
    await preferences_store.put(OLD_STATE_KEY, {"7": {
        "last_run_at": "2026-01-01T00:00:00Z",
        "last_queued": 1,
        "last_considered": 1,
        "converted": {"1": {"path": str(romm_env / "Game.rvz"), "pre": "",
                            "done": True}},
        "converted_ids": [2],
    }})

    got = await lib_routes.get_library_rules("romm")
    assert got["rules"]["7"]["mode"] == "dolphin_rvz"
    assert got["rules"]["7"]["duplicate_action"] == "overwrite"
    assert got["state"]["7"]["last_run_at"] == "2026-01-01T00:00:00Z"

    queued: list = []
    with _catalog(), _idle_queue(), _accepting_queue(queued):
        preview = await lib_routes.preview_library("romm", 
            lib_routes.SweepRequest(platform_ids=["7"]),
        )
    # Both ROMs are remembered as converted: ROM 1 by its record, ROM 2 by
    # the older id-only shape. Nothing reconverts after the upgrade.
    assert preview["queued"] == 0, preview
    assert preview["skipped_existing"] == 2


@pytest.mark.asyncio
async def test_changing_the_romm_instance_forgets_the_history(romm_env) -> None:
    await preferences_store.put(OLD_RULES_KEY, {"7": {
        "mode": "dolphin_rvz", "enabled": True, "duplicate_action": "overwrite",
    }})
    await preferences_store.put(OLD_STATE_KEY, {"7": {
        "last_run_at": "2026-01-01T00:00:00Z",
        "converted": {"1": {"path": "/x/Game.rvz", "pre": "", "done": True}},
    }})
    await romm_routes.put_romm_settings(
        romm_routes.RommSettingsPatch(url="http://another-romm:8080"),
    )
    got = await lib_routes.get_library_rules("romm")
    # The rules are the operator's configuration and survive; what they
    # believe they produced does not.
    assert got["rules"]["7"]["mode"] == "dolphin_rvz"
    assert "converted" not in got["state"]["7"]
    assert got["state"]["7"]["last_run_at"] == "2026-01-01T00:00:00Z"


def _ticks(count: int):
    """A stand-in for the scheduler's sleep that allows *count* ticks."""
    real_sleep = asyncio.sleep
    calls = 0

    async def _sleep(delay, *args, **kwargs):
        nonlocal calls
        if delay != 60:
            return await real_sleep(delay, *args, **kwargs)
        calls += 1
        if calls > count:
            raise asyncio.CancelledError
        return None

    return _sleep


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_on", [False, True])
async def test_the_scheduler_follows_the_master_switch(romm_env, switch_on) -> None:
    from services.romm import auto as romm_auto
    from services.romm import settings as romm_settings

    await romm_settings.save({"auto_convert": switch_on})
    await lib_routes.put_library_rules("romm", {"7": {"mode": "dolphin_rvz", "enabled": True}})
    queued: list = []
    with _catalog(), _idle_queue(), _accepting_queue(queued), \
            patch("asyncio.sleep", _ticks(1)), pytest.raises(asyncio.CancelledError):
        await romm_auto.run_forever()

    state = (await lib_routes.get_library_rules("romm"))["state"]
    if switch_on:
        assert len(queued) == 1
        assert state["7"]["last_queued"] == 2
    else:
        assert queued == []
        assert state == {}
