"""Converting an Image in a RomM Library keeps its RomM ROM id (#391).

With the Library's swap switch on (#390), a single-file Image's conversion
renames the ROM's file in RomM instead of letting RomM add a new ROM and keep a
ghost. The flow is the one spike #382 proved on RomM 5.4.1
(``docs/ROMM_INTEGRATION_ROADMAP.md``, "3.0 spike findings"): the file
Referrers first; ``PUT /api/roms/{id}`` with the output's ``fs_name`` (RomM
moves the Source to that name itself); a read-back of the name; park the
moved Source in the hidden staging folder and place the checked output at
that name; a ``hashes`` scan of that one ROM; a read-back of its size and
SHA-1; and only then the parked Source is deleted. Each step goes to the swap
journal before it runs.

The jobs run the real pipeline (convert route -> planner -> job manager) on a
temp Volume against :class:`tests.fake_romm.FakeRomm`; only the tool's convert,
post-convert and verify, and the Round-trip comparison, are stubbed.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio
from services import db as _db
from services import job_manager as job_manager_module
from services.round_trip import RoundTripOutcome
from utils.junk import STAGE_DIR_NAME

from app.models import (
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    JobCreateRequest,
    JobStatus,
)
from app.routes import convert as convert_routes

from .fake_romm import FakeRomm

PS2 = {"id": 3, "slug": "ps2", "fs_slug": "ps2", "name": "PlayStation 2"}
SOURCE_BYTES = b"ISO-" * 1024
OUTPUT_BYTES = b"CHD-output"
ROM_ID = 12
SCAN = {"type": "hashes", "roms_ids": [ROM_ID], "apis": []}


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    """A Volume holding RomM's library, ``romm/ps2/Game.iso``, and a database."""
    from services import libraries
    from services.romm import settings as romm_settings

    vol = tmp_path / "vol"
    (vol / "romm" / "ps2").mkdir(parents=True)
    (vol / "romm" / "ps2" / "Game.iso").write_bytes(SOURCE_BYTES)
    monkeypatch.setattr(convert_routes.settings, "chd_volumes", str(vol))
    monkeypatch.setattr(convert_routes.settings, "data_mount_root", str(vol))
    monkeypatch.setattr(convert_routes.settings, "temp_dir", str(tmp_path / "temp"))
    monkeypatch.setattr(convert_routes.settings, "data_dir", str(tmp_path / "config"))
    monkeypatch.setattr(convert_routes.settings, "max_queue_depth", 0)
    if _db.engine is not None:
        _db.engine.dispose()
    _db.init_engine(str(tmp_path / "compressatorium.db"), create_schema=True)
    romm_settings.reset_for_tests()
    libraries.reset_for_tests()
    yield vol
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


@pytest_asyncio.fixture(name="romm")
async def _romm(vol, monkeypatch):
    """The fake RomM, holding ROM 12 for ``ps2/Game.iso``, with the swap on."""
    from services.romm import settings as romm_settings

    fake = FakeRomm(platforms=[PS2], library=vol / "romm").start()
    fake.add_rom(ROM_ID, PS2, "Game.iso", igdb_id=4242)
    await romm_settings.save({
        "url": fake.url, "token": "rmm_x", "library_root": str(vol / "romm"),
        "swap_keep_rom_id": True,
    })
    _stub_tools(monkeypatch)
    yield fake
    fake.stop()


def _stub_tools(monkeypatch) -> None:
    async def fake_convert(input_path, output_path, mode, *, compression=None,
                           split=False, cancel_event=None):
        Path(output_path).write_bytes(OUTPUT_BYTES)
        yield {"progress": 100, "message": "done"}

    async def noop_post_convert(input_path, output_path, mode, *, within=None):
        return None

    async def fake_verify(path, *, cancel_event=None):
        return {"valid": True, "message": "ok"}

    async def fake_round_trip(tool, source_path, output_path, **_kw):
        return RoundTripOutcome(passed=True)

    for tool in convert_routes.registry.all():
        monkeypatch.setitem(vars(tool), "convert", fake_convert)
        monkeypatch.setitem(vars(tool), "post_convert", noop_post_convert)
        monkeypatch.setitem(vars(tool), "verify", fake_verify)
    monkeypatch.setattr(job_manager_module, "round_trip", fake_round_trip)
    monkeypatch.setattr(
        job_manager_module.registry, "mode_supports_round_trip", lambda _mode: True,
    )


async def _convert(source: Path, mode=ConversionMode.CREATEDVD):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=mode,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    await convert_routes.job_manager._process_job(job.id)
    return job


def _journal() -> list[dict]:
    from services.romm import swap_journal

    return swap_journal.rows()


def _platform(vol: Path) -> Path:
    return vol / "romm" / "ps2"


def _left_in_stage(vol: Path) -> list[str]:
    stage = _platform(vol) / STAGE_DIR_NAME
    return sorted(str(p.relative_to(stage)) for p in stage.rglob("*")) if stage.exists() else []


# ---- the swap ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_swap_keeps_the_rom_id_and_deletes_the_source(vol, romm):
    platform = _platform(vol)
    # What RomM would see, and what the journal said, as each call arrived.
    seen: list[tuple[str, str, list[str], str | None]] = []

    def observe(method, path):
        rows = _journal()
        seen.append((method, path.split("?", 1)[0], FakeRomm.scan_platform(platform),
                     rows[-1]["state"] if rows else None))

    romm.on_request = observe
    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    # The ROM kept its id, now names the output, and RomM hashed the new file.
    rom = romm.roms[ROM_ID]
    assert rom["fs_name"] == "Game.chd"
    assert rom["fs_size_bytes"] == len(OUTPUT_BYTES)
    assert rom["sha1_hash"] == hashlib.sha1(OUTPUT_BYTES).hexdigest()
    assert rom["igdb_id"] == 4242
    assert romm.renames == [(ROM_ID, "Game.chd")]
    assert romm.scans == [SCAN]
    # The Source is gone, the output in place, nothing left in the stage.
    assert sorted(p.name for p in platform.iterdir()) == ["Game.chd"]
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert _left_in_stage(vol) == []
    assert job.deleted_paths == [str(platform / "Game.iso")]
    # Each step was in the journal before RomM was asked to do it, and RomM
    # never saw the output beside the Source.
    calls = [(m, p, files, state) for m, p, files, state in seen if m != "GET"
             or p == f"/api/roms/{ROM_ID}"]
    assert calls[0] == ("PUT", f"/api/roms/{ROM_ID}", ["Game.iso"], "renaming")
    scan = next(c for c in calls if c[0] == "POST")
    assert scan == ("POST", "/api/tasks/scan", ["Game.chd"], "rehashing")
    # The journal row ends done, and the Ledger's record says the id was kept.
    (row,) = _journal()
    assert (row["state"], row["rom_id"]) == ("done", ROM_ID)
    (record,) = [r for r in job.referrers_updated if r["referrer"] == "romm_swap"]
    assert record["status"] == "updated"
    assert record["rom_id"] == ROM_ID
    assert record["kept_rom_id"] is True


@pytest.mark.asyncio
async def test_a_refused_rename_keeps_the_source_and_the_output(vol, romm):
    platform = _platform(vol)
    romm.rename_reply = (409, "Can't rename: Game.chd already exists")

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning == (
        "converted, source kept: RomM refused the rename "
        "(Can't rename: Game.chd already exists)"
    )
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert romm.scans == []
    assert job.deleted_paths == []
    assert [r["state"] for r in _journal()] == ["refused"]


@pytest.mark.asyncio
async def test_a_sanitized_name_is_a_refusal_and_the_old_name_is_put_back(vol, romm):
    platform = _platform(vol)
    # RomM silently writes another name than the one asked for.
    romm.rename_as = "Game_.chd"

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning == (
        "converted, source kept: RomM refused the rename "
        "(RomM named the file Game_.chd, not Game.chd)"
    )
    assert romm.renames == [(ROM_ID, "Game.chd"), (ROM_ID, "Game.iso")]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert sorted(p.name for p in platform.iterdir() if p.name != STAGE_DIR_NAME) == [
        "Game.chd", "Game.iso",
    ]
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert romm.scans == []
    assert [r["state"] for r in _journal()] == ["refused"]


@pytest.mark.asyncio
async def test_a_500_fails_the_swap_and_keeps_everything(vol, romm):
    platform = _platform(vol)
    romm.rename_reply = (500, "Failed to rename ROM 12 on disk")

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning.startswith("converted, source kept: ")
    assert "Failed to rename ROM 12 on disk" in job.warning
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert romm.scans == []
    assert [r["state"] for r in _journal()] == ["failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read_back", "task"])
async def test_a_failed_rehash_keeps_the_source_where_it_was(vol, romm, failure):
    platform = _platform(vol)
    if failure == "read_back":
        # The scan "finishes" but RomM still reports the Source's size and hash.
        romm.scan_updates = False
    else:
        romm.task_status = "failed"

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning.startswith("converted, source kept: "), job.warning
    assert romm.scans == [SCAN]
    # The Source is back under its own name, never deleted; the ROM keeps its
    # id on the checked output.
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert job.deleted_paths == []
    assert _left_in_stage(vol) == []
    assert [r["state"] for r in _journal()] == ["kept"]


@pytest.mark.asyncio
async def test_a_rule_sweep_swaps_too(vol, romm):
    from services.romm import auto as romm_auto

    platform = _platform(vol)
    await romm_auto.set_rules({str(PS2["id"]): {
        "enabled": True, "mode": "createdvd", "verify_after": True,
        "delete_on_verify": True, "check_level": "round_tripped",
    }})

    before = {job.id for job in convert_routes.job_manager.get_all_jobs()}
    result = await romm_auto.sweep(ignore_schedule=True)
    jobs = [j for j in convert_routes.job_manager.get_all_jobs() if j.id not in before]
    assert result.get("queued") == 1 and len(jobs) == 1, result
    await convert_routes.job_manager._process_job(jobs[0].id)

    job = jobs[0]
    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    assert romm.renames == [(ROM_ID, "Game.chd")]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert sorted(p.name for p in platform.iterdir()) == ["Game.chd"]
    assert [r["state"] for r in _journal()] == ["done"]


@pytest.mark.asyncio
async def test_an_unanswered_rename_is_put_back(vol, romm):
    platform = _platform(vol)
    dropped: list[str] = []

    def drop_the_answer(method, path):
        # RomM renames, then the answer is lost on the way back.
        if method == "PUT" and not dropped:
            dropped.append(path)
            romm._rename(ROM_ID, "Game.chd")
            raise ConnectionResetError("answer lost")

    romm.on_request = drop_the_answer
    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert "RomM didn't answer the rename" in job.warning
    # The old name was put back, and the Source moved back with it.
    assert romm.renames[-1] == (ROM_ID, "Game.iso")
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert romm.scans == []
    assert [r["state"] for r in _journal()] == ["rolled_back"]


@pytest.mark.asyncio
async def test_an_undo_that_fails_leaves_the_source_parked_for_the_next_run(vol, romm):
    from services import partial_writes

    platform = _platform(vol)
    romm.scan_updates = False

    def take_the_old_name(method, path):
        # While RomM rehashes, something takes the Source's own name, so the
        # parked Source can't be put back under it.
        if method == "POST":
            (platform / "Game.iso").write_bytes(b"someone else")

    romm.on_request = take_the_old_name
    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert "left for the next run" in job.warning
    assert job.deleted_paths == []
    (row,) = _journal()
    assert row["state"] == "restoring"
    assert "left for the next run" in row["detail"]
    # The Source is parked, untouched, and no sweep removes it.
    parked = Path(row["parked_path"])
    assert parked.read_bytes() == SOURCE_BYTES
    assert parked.parent.parent.name == STAGE_DIR_NAME
    hour_ago = time.time() - 3600
    os.utime(parked.parent, (hour_ago, hour_ago))
    partial_writes.sweep_recorded(min_age=0)
    assert partial_writes.sweep_folder(str(platform), min_age=0) == 0
    from services import staging

    staging.open_slot(str(platform)).close()  # opening a slot sweeps the stage
    assert parked.read_bytes() == SOURCE_BYTES


# ---- when the swap does not run ----------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_file_referrer_leaves_romm_untouched(vol, romm):
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"es_frontend": True})
    (platform / "gamelist.xml").write_text("<gameList><game><path>./Game.iso", "utf-8")

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert "gamelist.xml" in job.warning
    assert romm.renames == []
    assert romm.scans == []
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert _journal() == []


@pytest.mark.asyncio
async def test_with_the_switch_off_the_conversion_works_as_before(vol, romm):
    from services.romm import repin as romm_repin
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    row_id = romm_repin.record(
        romm.roms[ROM_ID], str(platform / "Game.chd"), {"igdb_id": 4242},
        "createdvd", source_path=str(platform / "Game.iso"),
    )

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None
    assert romm.renames == []
    assert sorted(p.name for p in platform.iterdir()) == ["Game.chd"]
    assert _journal() == []
    assert romm_repin.is_pending(row_id)


@pytest.mark.asyncio
async def test_a_successful_swap_settles_the_roms_re_pin(vol, romm):
    from services.romm import repin as romm_repin

    platform = _platform(vol)
    row_id = romm_repin.record(
        romm.roms[ROM_ID], str(platform / "Game.chd"), {"igdb_id": 4242},
        "createdvd", source_path=str(platform / "Game.iso"),
    )

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert not romm_repin.is_pending(row_id)
    assert romm_repin.count_pending() == 0


@pytest.mark.asyncio
async def test_a_multi_file_image_keeps_todays_behaviour(vol, romm):
    platform = _platform(vol)
    (platform / "Game.iso").unlink()
    romm.roms.clear()
    (platform / "Disc.bin").write_bytes(b"\0" * 2352)
    (platform / "Disc.cue").write_text(
        'FILE "Disc.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
        "utf-8",
    )
    romm.add_rom(ROM_ID, PS2, "Disc.cue")

    job = await _convert(platform / "Disc.cue", mode=ConversionMode.CREATECD)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert romm.renames == []
    assert sorted(p.name for p in platform.iterdir()) == ["Disc.chd"]
    assert _journal() == []


# ---- the delete confirmation --------------------------------------------------


@pytest.mark.asyncio
async def test_the_delete_confirmation_lists_the_romm_rename(vol, romm):
    platform = _platform(vol)

    plan = await convert_routes.delete_plan(DeletePlanRequest(
        file_paths=[str(platform / "Game.iso")], mode=ConversionMode.CREATEDVD,
    ))

    (item,) = plan["items"]
    swap = [c for c in item["referrer_changes"] if c["referrer"] == "romm_swap"]
    assert swap == [{
        "referrer": "romm_swap",
        "target": f"RomM ROM {ROM_ID}",
        "entries": 1,
        "edits": [{"old": "Game.iso", "new": "Game.chd"}],
        "summary": f"RomM: rename ROM {ROM_ID} to Game.chd",
    }]
    # Planning writes nothing.
    assert romm.renames == []
