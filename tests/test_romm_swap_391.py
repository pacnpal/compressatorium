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

import asyncio
import contextlib
import hashlib
import os
import threading
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
async def test_a_gamelist_rewrite_and_the_swap_share_the_stage_folder(vol, romm):
    """#404's Referrer temp file is written in a staging slot of its own
    beside the job's output slot; the swap then parks the Source in a third.
    Each holds its own lock, so none sweeps or closes another's."""
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"es_frontend": True})
    (platform / "gamelist.xml").write_text(
        "<gameList><game><path>./Game.iso</path><name>Game</name></game></gameList>\n",
        "utf-8",
    )
    (platform / "Game.m3u").write_text("Game.iso\n", "utf-8")

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    # Both file Referrers point at the output's final name, which it had not
    # taken yet while they ran (it was still staged for the swap).
    assert "<path>./Game.chd</path>" in (platform / "gamelist.xml").read_text("utf-8")
    assert (platform / "Game.m3u").read_text("utf-8") == "Game.chd\n"
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert not (platform / "Game.iso").exists()
    assert sorted(p.name for p in platform.iterdir() if p.suffix != ".bak") == [
        "Game.chd", "Game.m3u", "gamelist.xml",
    ]
    assert _left_in_stage(vol) == []
    assert [r["state"] for r in _journal()] == ["done"]


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


async def _convert_cancelling(source: Path, when: list[str]) -> object:
    """Convert *source*, pressing the job's Cancel from a blocking step's
    thread (*when* gets a callable to do it)."""
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()

    def cancel() -> None:
        event = convert_routes.job_manager.get_cancel_event(job.id)
        loop.call_soon_threadsafe(event.set)

    when.append(cancel)
    await convert_routes.job_manager._process_job(job.id)
    return job


@pytest.mark.asyncio
async def test_a_cancel_during_parking_is_undone_within_the_grace(vol, romm, monkeypatch):
    """Codex PRRT_kwDOQ9nULM64NxUt: a Cancel that lands while the Source is
    moved into the parking slot must not lose it. The move finishes and is
    recorded, then the swap's own undo runs within ``CANCEL_GRACE_SECONDS``
    (#383's grace): the Source goes back, RomM takes the old name back, and
    the row ends ``rolled_back``. The output is placed beside the Source."""
    from services import staging

    platform = _platform(vol)
    real_take = staging.StagedOutput.take
    cancel: list = []

    def slow_take(self, name):
        cancel[0]()
        time.sleep(0.5)  # the hook sees the Cancel while the move runs
        return real_take(self, name)

    monkeypatch.setattr(staging.StagedOutput, "take", slow_take)
    job = await _convert_cancelling(platform / "Game.iso", cancel)

    assert job.deleted_paths == []
    (row,) = _journal()
    assert row["state"] == "rolled_back", row
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert romm.renames == [(ROM_ID, "Game.chd"), (ROM_ID, "Game.iso")]
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert _left_in_stage(vol) == []


@pytest.mark.asyncio
async def test_a_cancel_whose_undo_outlasts_the_grace_leaves_the_source_parked(
    vol, romm, monkeypatch,
):
    """The undo after a Cancel gets ``CANCEL_GRACE_SECONDS``. One that stalls
    past it (here: putting the Source back hangs, then fails) leaves the row
    for the next run, the Source in its slot, never removed, and the output
    held back."""
    from services import partial_writes, referrers, staging

    platform = _platform(vol)
    monkeypatch.setattr(referrers, "CANCEL_GRACE_SECONDS", 0.5)
    real_take = staging.StagedOutput.take
    cancel: list = []

    def slow_take(self, name):
        cancel[0]()
        time.sleep(0.3)
        return real_take(self, name)

    def stalled_restore(self, name, as_name=None):
        time.sleep(1.5)
        raise OSError("the mount stopped answering")

    monkeypatch.setattr(staging.StagedOutput, "take", slow_take)
    monkeypatch.setattr(staging.StagedOutput, "restore", stalled_restore)
    job = await _convert_cancelling(platform / "Game.iso", cancel)
    # The undo outlives the hook's grace (``outlive``): wait for it to note.
    for _ in range(100):
        (row,) = _journal()
        if row["detail"] and "left for the next run" in row["detail"]:
            break
        await asyncio.sleep(0.05)

    assert job.deleted_paths == []
    assert row["state"] == "restoring", row
    assert "didn't finish within 0.5s" in row["detail"], row
    parked = Path(row["parked_path"])
    assert parked.read_bytes() == SOURCE_BYTES
    assert not (platform / "Game.chd").exists()
    hour_ago = time.time() - 3600
    os.utime(parked.parent, (hour_ago, hour_ago))
    partial_writes.sweep_recorded(min_age=0)
    staging.open_slot(str(platform)).close()
    assert parked.read_bytes() == SOURCE_BYTES


@pytest.mark.asyncio
async def test_a_slot_holding_the_source_is_never_removed_on_a_flags_word(
    vol, romm, monkeypatch,
):
    """The parking slot is removed only when its own listing is empty: a move
    that happened but reported a failure leaves the Source in the slot, kept,
    and the row says so."""
    from services import staging

    platform = _platform(vol)
    real_take = staging.StagedOutput.take

    def take_then_fail(self, name):
        real_take(self, name)
        raise OSError("the mount reported an error after the move")

    monkeypatch.setattr(staging.StagedOutput, "take", take_then_fail)
    job = await _convert(platform / "Game.iso")

    assert job.deleted_paths == []
    (row,) = _journal()
    assert "left for the next run" in row["detail"], row
    parked = Path(row["parked_path"])
    assert parked.read_bytes() == SOURCE_BYTES
    assert not (platform / "Game.chd").exists()


@pytest.mark.asyncio
async def test_a_romm_instance_change_mid_swap_renames_nothing_anywhere(
    vol, romm, monkeypatch,
):
    """Codex 4594286722: the swap is bound to the RomM instance it planned
    against. If the instance changes before the rename, nothing is renamed
    on either, and the row is left for the next run."""
    from services.romm import settings as romm_settings
    from services.romm import swap_referrer

    platform = _platform(vol)
    other = FakeRomm(platforms=[PS2], library=vol / "romm").start()
    other.add_rom(ROM_ID, PS2, "Game.iso")  # the other instance's ROM 12
    loop = asyncio.get_running_loop()
    real_hash = swap_referrer._size_and_sha1

    def hash_while_the_instance_moves(path):
        asyncio.run_coroutine_threadsafe(
            romm_settings.save({"url": other.url}, cleanup_pending=True), loop,
        ).result(5)
        return real_hash(path)

    monkeypatch.setattr(swap_referrer, "_size_and_sha1", hash_while_the_instance_moves)
    try:
        job = await _convert(platform / "Game.iso")
    finally:
        other.stop()

    assert romm.renames == [] and other.renames == []
    assert romm.scans == [] and other.scans == []
    (row,) = _journal()
    assert row["state"] == "renaming", row
    assert "RomM instance changed" in row["detail"]
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert job.deleted_paths == []


@pytest.mark.asyncio
async def test_a_romm_identity_change_waits_for_a_running_swap(vol, romm):
    """Codex 4594286722: a settings save that moves RomM's identity waits for
    a swap in flight to end, so the swap finishes on the instance it began."""
    from routes import romm as romm_routes

    platform = _platform(vol)
    other = FakeRomm(platforms=[PS2], library=vol / "romm").start()
    renaming = threading.Event()

    def slow_rename(method, path):
        if method == "PUT" and not renaming.is_set():
            renaming.set()
            time.sleep(0.8)

    romm.on_request = slow_rename
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    try:
        running = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
        while not renaming.is_set():
            await asyncio.sleep(0.02)
        save = asyncio.create_task(romm_routes._save_settings({"url": other.url}))
        await asyncio.sleep(0.4)
        assert not save.done()
        await running
        await asyncio.wait_for(save, 10)
    finally:
        other.stop()

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert other.renames == []
    assert [r["state"] for r in _journal()] == ["done"]


@pytest.mark.asyncio
async def test_cancelling_the_job_task_hands_its_holds_to_the_undo(vol, romm, monkeypatch):
    """Codex 4594286704: a shutdown cancels the job's task mid-swap. The
    undo outlives it, and the job's output lock and delete hold stay held
    until the undo ends."""
    from services import staging
    from services.lock_manager import lock_manager

    platform = _platform(vol)
    real_take, real_restore = staging.StagedOutput.take, staging.StagedOutput.restore
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()
    running = asyncio.create_task(convert_routes.job_manager._process_job(job.id))

    def take_then_shut_down(self, name):
        loop.call_soon_threadsafe(running.cancel)
        time.sleep(0.2)
        return real_take(self, name)

    def slow_restore(self, name, as_name=None):
        time.sleep(1.0)
        return real_restore(self, name, as_name)

    monkeypatch.setattr(staging.StagedOutput, "take", take_then_shut_down)
    monkeypatch.setattr(staging.StagedOutput, "restore", slow_restore)
    with pytest.raises(asyncio.CancelledError):
        await running
    source = os.path.realpath(platform / "Game.iso")

    # The job's task is gone; the swap (its move, then its undo) still runs,
    # and holds the Source.
    assert _journal()[0]["state"] in ("parking", "parked", "restoring"), _journal()
    assert lock_manager.is_locked(source)
    for _ in range(100):
        if _journal()[0]["state"] == "rolled_back" and not lock_manager.is_locked(source):
            break
        await asyncio.sleep(0.05)
    assert _journal()[0]["state"] == "rolled_back"
    assert not lock_manager.is_locked(source)
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES


@pytest.mark.asyncio
async def test_a_stalled_read_never_holds_a_shared_executor_thread(vol, romm, monkeypatch):
    """Codex PRRT_kwDOQ9nULM64NxU1: the swap's whole-file reads run on a
    detached thread, so a mount that stops answering strands that thread,
    never one of the event loop's shared executor's."""
    from services.romm import swap_referrer

    platform = _platform(vol)
    release = threading.Event()
    seen: list[tuple[str, bool]] = []
    cancel: list = []

    def stalled_sha1(path):
        seen.append((threading.current_thread().name, threading.current_thread().daemon))
        cancel[0]()
        release.wait(30)
        raise OSError("the mount stopped answering")

    monkeypatch.setattr(swap_referrer, "compute_file_sha1_sync", stalled_sha1)
    try:
        job = await asyncio.wait_for(_convert_cancelling(platform / "Game.iso", cancel), 20)
    finally:
        release.set()

    assert seen, "the swap never hashed the output"
    name, daemon = seen[0]
    assert daemon and not name.startswith("asyncio_"), seen
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert job.deleted_paths == []
    assert romm.renames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["roms_root", "platform_roms", "subfolder"])
async def test_the_swap_finds_the_rom_in_every_romm_layout(vol, romm, layout):
    """Codex PRRT_kwDOQ9nULM64NxUy: the Platform is the Library's Platform
    folder (``platform_folder``), not the Source's parent folder:
    ``<root>/roms/<platform>/Game.iso``, ``<root>/<platform>/roms/Game.iso``
    and a ROM in a subfolder of its Platform all keep their id."""
    root = vol / "romm"
    (root / "ps2" / "Game.iso").unlink()
    romm.roms.clear()
    folder, fs_path = {
        "roms_root": (root / "roms" / "ps2", "roms/ps2"),
        "platform_roms": (root / "ps2" / "roms", "ps2/roms"),
        "subfolder": (root / "ps2" / "Disc 1", "roms/ps2/Disc 1"),
    }[layout]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Game.iso").write_bytes(SOURCE_BYTES)
    romm.add_rom(ROM_ID, PS2, "Game.iso", fs_path=fs_path)

    job = await _convert(folder / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    assert romm.renames == [(ROM_ID, "Game.chd")]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert sorted(p.name for p in folder.iterdir()) == ["Game.chd"]
    assert [r["state"] for r in _journal()] == ["done"]


@pytest.mark.asyncio
async def test_a_rom_whose_record_is_its_folder_converts_as_before(vol, romm):
    """A file in a subfolder RomM records as one ROM (the folder): the record
    is the folder, which stays, so the swap doesn't apply."""
    root = vol / "romm"
    (root / "ps2" / "Game.iso").unlink()
    romm.roms.clear()
    folder = root / "ps2" / "Game"
    folder.mkdir()
    (folder / "Game.iso").write_bytes(SOURCE_BYTES)
    (folder / "Game.sav").write_bytes(b"save")
    romm.roms[ROM_ID] = {
        "id": ROM_ID, "name": "Game", "platform_id": PS2["id"], "platform_slug": "ps2",
        "fs_path": "roms/ps2", "fs_name": "Game", "full_path": "roms/ps2/Game",
        "has_multiple_files": True, "missing_from_fs": False,
    }

    job = await _convert(folder / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    assert romm.renames == []
    assert sorted(p.name for p in folder.iterdir()) == ["Game.chd", "Game.sav"]
    assert _journal() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unknowable", ["romm_unreadable", "two_records"])
async def test_a_rom_that_cant_be_told_keeps_the_source(vol, romm, unknowable):
    """With the swap on, a file in a RomM Platform folder whose ROM can't be
    told (RomM can't be read, or two records claim it) keeps the Source
    rather than delete it and orphan the record."""
    platform = _platform(vol)
    if unknowable == "romm_unreadable":
        romm.probe_status = 500
    else:
        romm.roms[ROM_ID + 1] = {**romm.roms[ROM_ID], "id": ROM_ID + 1}

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning.startswith("converted, source kept: "), job.warning
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert romm.renames == []
    assert job.deleted_paths == []


FALLBACK = (
    "the ROM did not keep its id: the swap can't run after the output was placed; "
    "re-pin queued"
)


@pytest.mark.asyncio
async def test_a_retried_delete_falls_back_to_the_re_pin_and_says_so(vol, romm, monkeypatch):
    """CodeRabbit 4593839018: a reader held the Source, so the job placed the
    output beside it and kept the Source. RomM can't rename onto a name that
    is taken, so the retried delete takes today's path, deleting the Source
    and queueing the re-pin, and says the ROM did not keep its id."""
    from services import db as db_module

    platform = _platform(vol)

    async def a_reader_holds_it(self, job_id, cancel_event):
        return False

    monkeypatch.setattr(
        job_manager_module.JobManager, "_hold_delete_paths_waiting", a_reader_holds_it,
    )
    job = await _convert(platform / "Game.iso")
    assert job.warning == job_manager_module.SOURCE_KEPT_FOR_READERS
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES

    outcome = await convert_routes.job_manager.retry_kept_delete(
        source_path=str(platform / "Game.iso"), output_path=str(platform / "Game.chd"),
    )

    assert outcome.status == job_manager_module.KEPT_DELETED, outcome
    assert FALLBACK in outcome.reason
    assert not (platform / "Game.iso").exists()
    assert romm.renames == []
    assert _journal() == []
    with db_module.SessionLocal() as session:
        rows = session.query(db_module.RommRepin).filter_by(state="pending").all()
    assert [(r.source_rom_id, r.output_path, r.metadata_ids) for r in rows] == [
        (ROM_ID, str(platform / "Game.chd"), {"igdb_id": 4242}),
    ]


@pytest.mark.asyncio
async def test_a_job_that_placed_its_output_first_says_the_rom_did_not_keep_its_id(
    vol, romm, monkeypatch,
):
    """The same fallback in a job whose output was placed before the hook:
    the Source is deleted, the re-pin queued, and the job's message and its
    Ledger record say the ROM did not keep its id."""
    platform = _platform(vol)

    async def no_staging(self, job, slot, write_path):
        return None

    monkeypatch.setattr(job_manager_module.JobManager, "_staged_placement", no_staging)
    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert FALLBACK in job.warning and FALLBACK in job.message
    assert job.deleted_paths == [str(platform / "Game.iso")]
    assert romm.renames == []
    (record,) = [r for r in job.referrers_updated if r["referrer"] == "romm_swap"]
    assert record["kept_rom_id"] is False
    assert FALLBACK in record["note"]


@pytest.mark.asyncio
async def test_a_duplicate_delete_is_not_a_failed_swap(vol, romm):
    """#389's duplicate delete runs the same delete step, the copy kept as
    its "output". That copy was not converted from the one deleted, so the
    swap stays out of it: no fallback note, no re-pin queued."""
    from services import db as db_module
    from services import referrers
    from services.romm.library import romm_library

    platform = _platform(vol)
    keeper = platform / "Game.chd"
    keeper.write_bytes(OUTPUT_BYTES)  # another copy, not converted from it
    source = str(platform / "Game.iso")

    outcome = await referrers.update_before_source_removal(
        source, {source: str(keeper)}, library=romm_library,
    )

    assert outcome.ok, outcome
    assert outcome.notes == []
    assert not [r for r in outcome.records if r["referrer"] == "romm_swap"]
    with db_module.SessionLocal() as session:
        assert session.query(db_module.RommRepin).count() == 0


@pytest.mark.asyncio
async def test_a_delete_check_that_fails_before_the_hook_keeps_the_output(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4593839025: the output is still staged when the delete step
    starts; a Source check that fails before the Referrer hook must not let
    the job's cleanup discard the checked output."""
    platform = _platform(vol)

    async def refuse(self, *args, **kwargs):
        raise RuntimeError("Delete path fingerprint mismatch; refusing to delete")

    monkeypatch.setattr(job_manager_module.JobManager, "_check_delete_paths", refuse)
    job = await _convert(platform / "Game.iso")

    assert "fingerprint mismatch" in (job.error_message or job.warning or "")
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert romm.renames == []


@pytest.mark.parametrize(("slug", "reported", "ok"), [
    ("ps2", "", False),            # a hashed Platform must report the SHA-1
    ("ps2", "0" * 40, False),
    ("switch", "", True),          # RomM never hashes Switch: size only
    ("switch", "0" * 40, False),   # but a SHA-1 it reports is compared
])
def test_the_read_back_compares_the_sha1_wherever_romm_reports_one(slug, reported, ok):
    """CodeRabbit 4593839047: size alone confirms a swap only on a Platform
    RomM never hashes, and only when it reports no SHA-1."""
    from services.romm.swap_referrer import _Plan, _read_back_problem

    plan = _Plan(ROM_ID, "/l/Game.iso", "/l/Game.chd", "Game.iso", "Game.chd", slug)
    rom = {"id": ROM_ID, "fs_name": "Game.chd", "fs_size_bytes": 10,
           "platform_slug": slug, "sha1_hash": reported}

    assert (_read_back_problem(rom, plan, 10, "f" * 40) is None) is ok


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", [True, False])
async def test_a_checked_output_whose_name_is_taken_is_kept_in_its_slot(
    vol, romm, monkeypatch, swap,
):
    """CodeRabbit 4593970363: a file appears at the output's name after the
    checks, so the checked output can't be placed. It is kept in its slot,
    out of every sweep, the Source stays, and the job says where."""
    from services import partial_writes, staging
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": swap})

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await _convert(platform / "Game.iso")

    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == b"someone else's"
    assert job.deleted_paths == []
    kept = [p for p in (platform / STAGE_DIR_NAME).rglob("Game.chd")]
    assert [p.read_bytes() for p in kept] == [OUTPUT_BYTES], kept
    assert f"kept at {kept[0]}" in job.message
    assert any(r.get("status") == "kept" for r in job.referrers_updated)
    # Its check record moved with it (CodeRabbit 4594086355).
    from services.verification_store import verification_store

    assert verification_store.get_record_blocking(str(kept[0])) is not None
    hour_ago = time.time() - 3600
    os.utime(kept[0].parent, (hour_ago, hour_ago))
    partial_writes.sweep_recorded(min_age=0)
    staging.open_slot(str(platform)).close()
    assert kept[0].read_bytes() == OUTPUT_BYTES


@pytest.mark.asyncio
async def test_a_set_aside_that_fails_still_keeps_the_output_from_every_sweep(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594086355: if the slot can't be renamed to a ``park-``
    slot, it is marked kept in place, so no sweep removes the only copy of
    a checked output once the job lets go of its lock."""
    from services import partial_writes, staging
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    real_place = staging.place_without_clobbering

    def no_rename_to_park(src, dst, **kwargs):
        if str(dst).startswith(staging.PARK_PREFIX):
            raise OSError("the mount refused the rename")
        return real_place(src, dst, **kwargs)

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(staging, "place_without_clobbering", no_rename_to_park)
    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await _convert(platform / "Game.iso")

    (kept,) = (platform / STAGE_DIR_NAME).glob("slot-*/Game.chd")
    assert kept.read_bytes() == OUTPUT_BYTES
    assert f"kept at {kept}" in job.message
    hour_ago = time.time() - 3600
    os.utime(kept.parent, (hour_ago, hour_ago))
    partial_writes.sweep_recorded(min_age=0)
    monkeypatch.setattr(staging, "place_without_clobbering", real_place)
    staging.open_slot(str(platform)).close()  # opening a slot sweeps the stage
    assert kept.read_bytes() == OUTPUT_BYTES
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES


def test_a_keep_marker_written_while_the_sweep_takes_the_lock_still_keeps_the_slot(
    vol, monkeypatch,
):
    """CodeRabbit 4594218526: the marker is written, and the lock let go,
    between the sweep's first look and its lock. The sweep looks again under
    the lock and keeps the slot."""
    from services import partial_writes

    stage = _platform(vol) / STAGE_DIR_NAME
    slot = stage / "slot-abcd1234"
    slot.mkdir(parents=True)
    (slot / "Game.chd").write_bytes(OUTPUT_BYTES)
    (stage / "slot-abcd1234.lock").write_bytes(b"")
    hour_ago = time.time() - 3600
    os.utime(slot, (hour_ago, hour_ago))
    real_open = partial_writes._open_sidecar

    def marked_meanwhile(stage_fd, lock_name):
        (slot / partial_writes.KEEP_MARKER).write_bytes(b"")
        return real_open(stage_fd, lock_name)

    monkeypatch.setattr(partial_writes, "_open_sidecar", marked_meanwhile)
    fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert partial_writes.sweep_stage(fd, min_age=0) == 0
    finally:
        os.close(fd)
    assert (slot / "Game.chd").read_bytes() == OUTPUT_BYTES


@pytest.mark.asyncio
async def test_a_set_aside_that_answers_after_the_bound_still_moves_the_records(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594218526: setting the slot aside outlasts the job's bound.
    The slot stays protected meanwhile (its lock held), and when the worker
    answers, the records move to the ``park-`` path and the job is told the
    real path."""
    from services import staging
    from services.romm import settings as romm_settings
    from services.verification_store import verification_store

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    real_set_aside = staging.StagedOutput.set_aside

    def slow_set_aside(self):
        time.sleep(0.6)
        return real_set_aside(self)

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(staging.StagedOutput, "set_aside", slow_set_aside)
    monkeypatch.setattr(job_manager_module, "SET_ASIDE_SECONDS", 0.2)
    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await _convert(platform / "Game.iso")

    assert str(platform / STAGE_DIR_NAME) in job.message
    kept: list[Path] = []
    for _ in range(60):
        kept = list((platform / STAGE_DIR_NAME).glob("park-*/Game.chd"))
        if kept and verification_store.get_record_blocking(str(kept[0])) and str(
            kept[0],
        ) in job.message:
            break
        await asyncio.sleep(0.05)
    assert [p.read_bytes() for p in kept] == [OUTPUT_BYTES]
    assert verification_store.get_record_blocking(str(kept[0])) is not None
    assert str(kept[0]) in job.message


@pytest.mark.asyncio
async def test_a_failed_source_check_then_a_taken_name_keeps_everything(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594086355: the Source check fails before the hook, then
    placing the checked output finds its name taken. The output is kept with
    its check record, and the Source stays."""
    from services.verification_store import verification_store

    platform = _platform(vol)

    async def refuse(self, *args, **kwargs):
        (platform / "Game.chd").write_bytes(b"someone else's")
        raise RuntimeError("Delete path fingerprint mismatch; refusing to delete")

    monkeypatch.setattr(job_manager_module.JobManager, "_check_delete_paths", refuse)
    job = await _convert(platform / "Game.iso")

    assert "fingerprint mismatch" in (job.error_message or "")
    (kept,) = (platform / STAGE_DIR_NAME).glob("park-*/Game.chd")
    assert kept.read_bytes() == OUTPUT_BYTES
    assert verification_store.get_record_blocking(str(kept)) is not None
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == b"someone else's"


@pytest.mark.asyncio
async def test_a_cancel_during_set_aside_still_releases_and_announces_the_job(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594326475: a cancel while the job's ``finally`` sets the
    checked output aside must not skip the terminal notice, the lock
    releases or the concurrency slot."""
    from services import staging
    from services.concurrency_manager import concurrency_manager
    from services.lock_manager import lock_manager
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    real_set_aside = staging.StagedOutput.set_aside
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()
    running = asyncio.create_task(convert_routes.job_manager._process_job(job.id))

    def cancelled_set_aside(self):
        loop.call_soon_threadsafe(running.cancel)
        time.sleep(0.3)
        return real_set_aside(self)

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(staging.StagedOutput, "set_aside", cancelled_set_aside)
    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    with pytest.raises(asyncio.CancelledError):
        await running

    manager = convert_routes.job_manager
    assert job.status in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.COMPLETED)
    assert job.id in manager._terminal_announced
    for _ in range(40):
        if not lock_manager.is_locked(str(platform / "Game.chd")):
            break
        await asyncio.sleep(0.05)
    assert not lock_manager.is_locked(str(platform / "Game.chd"))
    assert not lock_manager.is_locked(os.path.realpath(platform / "Game.iso"))
    assert job.id not in concurrency_manager._lock_handles
    # The output is kept all the same, once its slot is set aside.
    for _ in range(40):
        if list((platform / STAGE_DIR_NAME).glob("park-*/Game.chd")):
            break
        await asyncio.sleep(0.05)
    assert [p.read_bytes() for p in (platform / STAGE_DIR_NAME).glob("park-*/Game.chd")] == [
        OUTPUT_BYTES,
    ]


@pytest.mark.asyncio
async def test_a_set_aside_with_no_thread_at_all_marks_the_slot_and_ends_the_job(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594745731: no detached thread is free and a thread of its
    own can't start either (the OS thread limit). The slot is still marked
    kept, inline, and the job still announces its end and lets go of every
    lock."""
    from services import partial_writes, subprocess_runner
    from services.lock_manager import lock_manager
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    real_start = threading.Thread.start

    def no_slot_free(*_args, **_kwargs):
        raise subprocess_runner.ProbeCapacityExceeded("no detached thread free")

    def thread_limit(self):
        if getattr(self, "_target", None) is not None and self._target.__qualname__.startswith(
            "start_detached_or_own_thread",
        ):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(subprocess_runner, "start_detached", no_slot_free)
    monkeypatch.setattr(threading.Thread, "start", thread_limit)
    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    with contextlib.suppress(Exception):
        await convert_routes.job_manager._process_job(job.id)

    assert job.id in convert_routes.job_manager._terminal_announced
    assert not lock_manager.is_locked(str(platform / "Game.chd"))
    assert not lock_manager.is_locked(os.path.realpath(platform / "Game.iso"))
    (kept,) = (platform / STAGE_DIR_NAME).glob("slot-*/Game.chd")
    assert kept.read_bytes() == OUTPUT_BYTES
    assert (kept.parent / partial_writes.KEEP_MARKER).exists()
    assert f"kept at {kept}" in job.message


@pytest.mark.asyncio
async def test_a_set_aside_with_no_detached_thread_free_still_keeps_the_output(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594613612: with every detached thread in use, the
    set-aside still starts before the first await a cancel can interrupt,
    on a thread of its own; a cancel during the listing, a restart and the
    sweeps leave the output kept."""
    from services import subprocess_runner

    def no_slot_free(*_args, **_kwargs):
        raise subprocess_runner.ProbeCapacityExceeded("no detached thread free")

    monkeypatch.setattr(subprocess_runner, "start_detached", no_slot_free)
    monkeypatch.setattr(job_manager_module, "start_detached", no_slot_free)
    await test_a_cancel_before_the_set_aside_starts_still_keeps_the_output(
        vol, romm, monkeypatch,
    )


@pytest.mark.asyncio
async def test_a_cancel_before_the_set_aside_starts_still_keeps_the_output(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594495308: a cancel during the first probe of preserving
    a checked output (listing its names) must not leave its slot merely
    locked. After a "restart" (every slot's lock let go) and the sweeps, the
    output is still there."""
    from services import partial_writes, staging
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"swap_keep_rom_id": False})
    opened: list[staging.StagedOutput] = []
    real_open = staging.open_slot

    def recording_open(*args, **kwargs):
        slot = real_open(*args, **kwargs)
        opened.append(slot)
        return slot

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(staging, "open_slot", recording_open)
    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()
    running = asyncio.create_task(convert_routes.job_manager._process_job(job.id))
    real_names = job_manager_module.JobManager._place_names
    probes: list[str] = []

    def cancelled_probe(self, job_, write_path):
        # The first listings are the placing decision's and the placing's
        # (which fails: the name is taken); the third is the set-aside's,
        # which the cancel cuts short.
        probes.append(write_path)
        if len(probes) == 3:
            loop.call_soon_threadsafe(running.cancel)
            time.sleep(0.3)
        return real_names(self, job_, write_path)

    monkeypatch.setattr(job_manager_module.JobManager, "_place_names", cancelled_probe)
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(0.5)  # a set-aside the cancel cut short finishes on its worker
    # A "restart": every slot this process still holds lets go of its lock.
    for slot in opened:
        slot.keep()
    stage = platform / STAGE_DIR_NAME
    for entry in stage.iterdir():
        hour_ago = time.time() - 3600
        os.utime(entry, (hour_ago, hour_ago))
    partial_writes.sweep_recorded(min_age=0)
    real_open(str(platform)).close()  # opening a slot sweeps the stage folder

    kept = list(stage.rglob("Game.chd"))
    assert [p.read_bytes() for p in kept] == [OUTPUT_BYTES], kept
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES


@pytest.mark.asyncio
async def test_a_lingering_swap_keeps_the_job_from_placing_the_output(
    vol, romm, monkeypatch,
):
    """Codex 4594772381: a Cancel lands while the Source is parked, and the
    parking's journal write outlives the hook's grace, so the swap lingers.
    The job must not place the output meanwhile: the swap's undo then puts
    the Source back under its old name (``rolled_back``), and the checked
    output is kept, set aside, not over the Source's way back."""
    from services import referrers, staging
    from services.romm import swap_journal

    platform = _platform(vol)
    monkeypatch.setattr(referrers, "CANCEL_GRACE_SECONDS", 0.3)
    real_take, real_step = staging.StagedOutput.take, swap_journal.step
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()

    def cancelled_take(self, name):
        event = convert_routes.job_manager.get_cancel_event(job.id)
        loop.call_soon_threadsafe(event.set)
        return real_take(self, name)

    def slow_parked(row_id, state, **kwargs):
        if state == "parked":
            time.sleep(1.0)  # outlives the hook's grace
        return real_step(row_id, state, **kwargs)

    monkeypatch.setattr(staging.StagedOutput, "take", cancelled_take)
    monkeypatch.setattr(swap_journal, "step", slow_parked)
    await convert_routes.job_manager._process_job(job.id)
    for _ in range(80):
        if [r["state"] for r in _journal()] == ["rolled_back"]:
            break
        await asyncio.sleep(0.05)

    assert [r["state"] for r in _journal()] == ["rolled_back"], _journal()
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"
    assert not (platform / "Game.chd").exists()
    kept = list((platform / STAGE_DIR_NAME).glob("park-*/Game.chd"))
    assert [p.read_bytes() for p in kept] == [OUTPUT_BYTES]


@pytest.mark.asyncio
async def test_a_cancel_during_placing_waits_for_it_and_keeps_the_holds(
    vol, romm, monkeypatch,
):
    """Codex 4594650576: a Cancel lands while the swap places the output.
    Placing is never abandoned: the swap waits for its real outcome (here,
    placed), undoes accordingly (the Source back beside the output, ``kept``),
    and no hold or lock of the job is let go before that undo has ended."""
    from services import staging
    from services.romm import swap_journal

    platform = _platform(vol)
    source = os.path.realpath(platform / "Game.iso")
    events: list[str] = []
    real_place, real_step = staging.StagedOutput.place, swap_journal.step
    real_let_go = job_manager_module._let_go
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()

    def cancelled_place(self, names):
        names = list(names)
        if "Game.chd" in names and not events:
            event = convert_routes.job_manager.get_cancel_event(job.id)
            loop.call_soon_threadsafe(event.set)
            events.append("placing")
            time.sleep(0.5)
        return real_place(self, names)

    def recording_step(row_id, state, **kwargs):
        if state in swap_journal.ENDINGS:
            events.append(f"ended {state}")
        return real_step(row_id, state, **kwargs)

    def recording_let_go(paths):
        paths = list(paths or ())
        if source in paths:
            events.append("released the Source")
        return real_let_go(paths)

    monkeypatch.setattr(staging.StagedOutput, "place", cancelled_place)
    monkeypatch.setattr(swap_journal, "step", recording_step)
    monkeypatch.setattr(job_manager_module, "_let_go", recording_let_go)
    await convert_routes.job_manager._process_job(job.id)
    for _ in range(60):
        if "released the Source" in events:
            break
        await asyncio.sleep(0.05)

    assert events[0] == "placing", events
    assert events.index("ended kept") < events.index("released the Source"), events
    assert [r["state"] for r in _journal()] == ["kept"]
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert (platform / "Game.chd").read_bytes() == OUTPUT_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"


@pytest.mark.asyncio
async def test_a_source_changed_during_the_rom_lookup_is_never_swapped(
    vol, romm, monkeypatch,
):
    """Codex 4594537162: the swap's baseline is the Source's pre-conversion
    fingerprint (the delete snapshot), not a fresh stat. A Source replaced
    while the ROM is looked up is not renamed, parked or deleted."""
    from services.romm import swap_referrer

    platform = _platform(vol)
    real_lookup = swap_referrer._rom_for

    def lookup_while_replaced(source):
        replacement = platform / ".replacement"
        replacement.write_bytes(b"another disc altogether")
        os.replace(replacement, source)
        return real_lookup(source)

    monkeypatch.setattr(swap_referrer, "_rom_for", lookup_while_replaced)
    job = await _convert(platform / "Game.iso")

    assert romm.renames == []
    assert job.deleted_paths == []
    assert (platform / "Game.iso").read_bytes() == b"another disc altogether"
    assert job.warning and job.warning.startswith("converted, source kept"), job.warning


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["settle", "done"])
async def test_a_cancel_after_the_source_is_deleted_still_reports_it_deleted(
    vol, romm, monkeypatch, during,
):
    """Codex 4594537178: once the parked Source is deleted, a Cancel during
    the bookkeeping after it (settling the re-pin, recording ``done``) never
    loses the swap's result: the job records the Source as deleted."""
    from services.romm import swap_journal, swap_referrer

    platform = _platform(vol)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()

    def cancel_now():
        event = convert_routes.job_manager.get_cancel_event(job.id)
        loop.call_soon_threadsafe(event.set)
        time.sleep(0.3)

    if during == "settle":
        real_settle = swap_referrer._settle_repin

        def cancelled_settle(output, rom_id):
            cancel_now()
            return real_settle(output, rom_id)

        monkeypatch.setattr(swap_referrer, "_settle_repin", cancelled_settle)
    else:
        real_step = swap_journal.step

        def cancelled_done(row_id, state, **kwargs):
            if state == "done":
                cancel_now()
            return real_step(row_id, state, **kwargs)

        monkeypatch.setattr(swap_journal, "step", cancelled_done)
    await convert_routes.job_manager._process_job(job.id)

    assert job.deleted_paths == [str(platform / "Game.iso")]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    for _ in range(40):
        if [r["state"] for r in _journal()] == ["done"]:
            break
        await asyncio.sleep(0.05)
    assert [r["state"] for r in _journal()] == ["done"]


@pytest.mark.asyncio
async def test_a_cancel_while_the_parked_source_is_deleted_ends_done(vol, romm, monkeypatch):
    """CodeRabbit 4594326517: the Cancel lands while the parked Source is
    removed. The removal is recorded on its own thread, so the swap ends
    ``done`` and the job records the Source as deleted, not kept."""
    from services import staging

    platform = _platform(vol)
    real_remove = staging.StagedOutput.remove
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(platform / "Game.iso"), mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=True,
    ))
    loop = asyncio.get_running_loop()

    def cancelled_remove(self, name):
        event = convert_routes.job_manager.get_cancel_event(job.id)
        loop.call_soon_threadsafe(event.set)
        time.sleep(0.3)
        return real_remove(self, name)

    monkeypatch.setattr(staging.StagedOutput, "remove", cancelled_remove)
    await convert_routes.job_manager._process_job(job.id)

    assert [r["state"] for r in _journal()] == ["done"]
    assert romm.roms[ROM_ID]["fs_name"] == "Game.chd"
    assert job.deleted_paths == [str(platform / "Game.iso")]
    assert sorted(p.name for p in platform.iterdir()) == ["Game.chd"]


@pytest.mark.asyncio
async def test_a_re_pin_settle_that_times_out_still_reports_the_swap(vol, romm, monkeypatch):
    """CodeRabbit 4594326492: settling the re-pin is bookkeeping. A timeout
    there, after the Source is gone, still reports the swap as applied."""
    from services.romm import swap_referrer
    from services.subprocess_runner import DeadlineExpired

    platform = _platform(vol)
    real_probe = swap_referrer._probe

    async def settle_times_out(func, *args, **kwargs):
        if func is swap_referrer._settle_repin:
            raise DeadlineExpired("no answer in 20s")
        return await real_probe(func, *args, **kwargs)

    monkeypatch.setattr(swap_referrer, "_probe", settle_times_out)
    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.warning is None, job.warning
    assert job.deleted_paths == [str(platform / "Game.iso")]
    assert [r["state"] for r in _journal()] == ["done"]


@pytest.mark.asyncio
async def test_a_failed_referrer_is_recorded_even_when_placing_then_fails(
    vol, romm, monkeypatch,
):
    """CodeRabbit 4594326468: a file Referrer fails, then placing the checked
    output finds its name taken. The Referrer's failure stays in the job's
    record (and so the Ledger's)."""
    from services.romm import settings as romm_settings

    platform = _platform(vol)
    await romm_settings.save({"es_frontend": True})
    (platform / "gamelist.xml").write_text("<gameList><game><path>./Game.iso", "utf-8")

    async def checked_then_taken(tool, source_path, output_path, **_kw):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return RoundTripOutcome(passed=True)

    monkeypatch.setattr(job_manager_module, "round_trip", checked_then_taken)
    job = await _convert(platform / "Game.iso")

    failed = [r for r in job.referrers_updated if r.get("referrer") == "gamelist"]
    assert failed and failed[0]["status"] == "failed", job.referrers_updated
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES


def test_the_fake_lists_roms_a_page_at_a_time(vol, romm):
    """CodeRabbit 4593839062: like RomM, the listing honours ``limit`` and
    ``offset``, and ``total`` counts every ROM the filter matches."""
    from services.romm import RommClient

    romm.add_rom(ROM_ID + 1, PS2, "Game.iso")
    client = RommClient(base_url=romm.url, token="rmm_x")

    page = client._request(
        "GET", "/api/roms", params={"platform_ids": PS2["id"], "limit": 1, "offset": 1},
    )

    assert [rom["id"] for rom in page["items"]] == [ROM_ID + 1]
    assert page["total"] == 2


def test_the_fake_never_moves_a_file_out_of_its_folder(vol, romm):
    """RomM never moves a ROM's file out of its Platform folder: a name with a
    path in it is sanitized to its last part (spike #382), and a name that
    still points elsewhere is refused (CodeQL py/path-injection)."""
    from services.romm import RommClient, RommError

    platform = _platform(vol)
    client = RommClient(base_url=romm.url, token="rmm_x")

    with pytest.raises(RommError) as refused:
        client.rename_rom(ROM_ID, "..")
    assert refused.value.status == 400
    assert (platform / "Game.iso").read_bytes() == SOURCE_BYTES
    assert romm.roms[ROM_ID]["fs_name"] == "Game.iso"

    assert client.rename_rom(ROM_ID, "../escape.iso")["fs_name"] == "escape.iso"
    assert (platform / "escape.iso").read_bytes() == SOURCE_BYTES
    assert not (vol / "romm" / "escape.iso").exists()


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
