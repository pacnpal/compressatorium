"""In-progress conversion output stays hidden from RomM scans (#398).

The swap spike (#382) found that RomM 5.4.1 indexes dot-prefixed *files* but
skips dot-prefixed *folders*. A RomM scan that sees a conversion's output (or
its temp file) beside the Source makes a duplicate ROM, and a ghost once the
Source goes. So for Images in a RomM Library the job writes into a hidden
staging folder beside the output (``services.staging``) and moves the output
into place only after the Intact and Round-trip checks pass.

The jobs run the real pipeline (convert route -> planner -> job manager) on a
temp Volume; only the tool's convert, post-convert and verify, and the
Round-trip comparison, are stubbed. ``FakeRomm.scan_platform`` lists the
Platform folder by RomM 5.4.1's rule.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from services import db as _db
from services import job_manager as job_manager_module
from services import partial_writes, staging
from services.round_trip import RoundTripOutcome
from utils.junk import STAGE_DIR_NAME, is_junk_entry

from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
from app.routes import convert as convert_routes

from .fake_romm import FakeRomm

HOUR_AGO = time.time() - 3600
APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(name="vol")
def _vol(tmp_path: Path, monkeypatch):
    """A Volume holding RomM's library (``romm/ps2/Game.iso``) and a folder
    Library (``roms/ps2/Game.iso``), with a database and stubbed tools."""
    from services import libraries
    from services.romm import client as romm_client_module
    from services.romm import settings as romm_settings

    vol = tmp_path / "vol"
    for platform in (vol / "romm" / "ps2", vol / "roms" / "ps2", vol / "loose"):
        platform.mkdir(parents=True)
        (platform / "Game.iso").write_bytes(b"\0" * 4096)
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
    monkeypatch.setattr(
        type(romm_client_module.romm_client), "library_root",
        property(lambda _self: str((vol / "romm").resolve())),
    )
    libraries.register(str(vol / "roms"))
    yield vol
    libraries.reset_for_tests()
    romm_settings.reset_for_tests()
    if _db.engine is not None:
        _db.engine.dispose()
    _db.engine = None
    _db.SessionLocal = None


class _Tools:
    """Stubbed convert, verify and Round-trip that look at the Platform folder
    the way a RomM scan would, every time they run."""

    def __init__(self, platform: Path, *, valid: bool = True, round_trip: bool = True):
        self.platform = platform
        self.valid = valid
        self.round_trip_passes = round_trip
        self.scans: list[tuple[str, list[str]]] = []
        self.written_to: list[str] = []
        self.verified: list[str] = []
        self.compared: list[str] = []

    def scan(self, when: str) -> None:
        self.scans.append((when, FakeRomm.scan_platform(self.platform)))

    def install(self, monkeypatch) -> None:
        tools = self

        async def fake_convert(input_path, output_path, mode, *, compression=None,
                               split=False, cancel_event=None):
            tools.written_to.append(output_path)
            out = Path(output_path)
            # A tool's own temp file beside its output, then the output itself.
            temp = out.with_name(f".{out.name}.partial-abcd1234.tmp")
            temp.write_bytes(b"half")
            tools.scan("temp file written")
            temp.unlink()
            out.write_bytes(b"CHD")
            tools.scan("output written")
            yield {"progress": 100, "message": "done"}

        async def noop_post_convert(input_path, output_path, mode, *, within=None):
            return None

        async def fake_verify(path, *, cancel_event=None):
            tools.verified.append(path)
            tools.scan("intact check")
            return {"valid": tools.valid, "message": "ok" if tools.valid else "bad"}

        async def fake_round_trip(tool, source_path, output_path, **_kw):
            tools.compared.append(output_path)
            tools.scan("round-trip check")
            if tools.round_trip_passes:
                return RoundTripOutcome(passed=True)
            return RoundTripOutcome(passed=False, message="the contents differ", mismatch=True)

        for tool in convert_routes.registry.all():
            monkeypatch.setitem(vars(tool), "convert", fake_convert)
            monkeypatch.setitem(vars(tool), "post_convert", noop_post_convert)
            monkeypatch.setitem(vars(tool), "verify", fake_verify)
        monkeypatch.setattr(job_manager_module, "round_trip", fake_round_trip)
        monkeypatch.setattr(
            job_manager_module.registry, "mode_supports_round_trip", lambda _mode: True,
        )


async def _convert(source: Path, *, verify_after: bool = False, **options):
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(source),
        mode=ConversionMode.CREATEDVD,
        duplicate_action=DuplicateAction.SKIP,
        **options,
    ))
    # Verify and keep the Source: what a Rule's verify_after asks (the
    # convert route has no such field).
    job.verify_after = verify_after
    await convert_routes.job_manager._process_job(job.id)
    return job


# ---- the hidden folder ------------------------------------------------------


def test_the_stage_folder_is_hidden_from_listings_and_walks(tmp_path):
    assert STAGE_DIR_NAME.startswith(".")
    assert is_junk_entry(STAGE_DIR_NAME)
    assert is_junk_entry(STAGE_DIR_NAME.upper())
    assert not is_junk_entry("compressatorium-stage")

    from utils.path_utils import walk_tree

    (tmp_path / STAGE_DIR_NAME / "slot-abcd1234").mkdir(parents=True)
    (tmp_path / STAGE_DIR_NAME / "slot-abcd1234" / "Game.chd").write_bytes(b"x")
    (tmp_path / "Game.iso").write_bytes(b"x")
    walked = [(Path(folder).name, dirs, files) for folder, dirs, files
              in walk_tree(str(tmp_path), lambda *_a, **_k: None)]
    assert walked == [(tmp_path.name, [], ["Game.iso"])]


def test_romm_declares_hidden_staging_and_folder_libraries_do_not():
    from services.libraries.folder import FolderLibrary
    from services.romm.library import romm_library
    from services.rules.library import HIDDEN_STAGING

    assert HIDDEN_STAGING in romm_library.capabilities
    assert HIDDEN_STAGING not in FolderLibrary.capabilities


# ---- a conversion in a RomM Library ---------------------------------------


@pytest.mark.asyncio
async def test_a_romm_scan_mid_conversion_never_sees_the_output_or_a_temp_file(
    vol, monkeypatch,
):
    platform = vol / "romm" / "ps2"
    tools = _Tools(platform)
    tools.install(monkeypatch)

    job = await _convert(platform / "Game.iso", delete_on_verify=True)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert job.check_passed == "round_tripped"
    # Every scan before the checks passed saw only the Source.
    assert [when for when, _ in tools.scans] == [
        "temp file written", "output written", "intact check", "round-trip check",
    ]
    assert all(seen == ["Game.iso"] for _, seen in tools.scans), tools.scans
    # The tool wrote, and both checks read, inside the hidden folder.
    stage = platform / STAGE_DIR_NAME
    for path in (*tools.written_to, *tools.verified, *tools.compared):
        assert Path(path).parent.parent == stage, path
    # Then the output was placed, the Source deleted, and nothing is left over.
    assert FakeRomm.scan_platform(platform) == ["Game.chd"]
    assert (platform / "Game.chd").read_bytes() == b"CHD"
    assert not stage.exists()
    assert job.output_path == str(platform / "Game.chd")


@pytest.mark.asyncio
async def test_without_checks_the_output_is_placed_once_converted(vol, monkeypatch):
    platform = vol / "romm" / "ps2"
    tools = _Tools(platform)
    tools.install(monkeypatch)

    job = await _convert(platform / "Game.iso")

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert all(seen == ["Game.iso"] for _, seen in tools.scans), tools.scans
    assert FakeRomm.scan_platform(platform) == ["Game.chd", "Game.iso"]
    assert not (platform / STAGE_DIR_NAME).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["intact", "round_trip"])
async def test_an_output_that_fails_a_check_never_appears(vol, monkeypatch, failure):
    platform = vol / "romm" / "ps2"
    tools = _Tools(
        platform, valid=failure != "intact", round_trip=failure != "round_trip",
    )
    tools.install(monkeypatch)

    job = await _convert(platform / "Game.iso", delete_on_verify=True)

    assert job.status == JobStatus.FAILED
    assert all(seen == ["Game.iso"] for _, seen in tools.scans), tools.scans
    assert FakeRomm.scan_platform(platform) == ["Game.iso"]
    assert not (platform / STAGE_DIR_NAME).exists()


@pytest.mark.asyncio
async def test_an_existing_file_at_the_destination_is_never_overwritten_by_placing(
    vol, monkeypatch,
):
    """Something took the output's name while the job converted: the placing
    refuses and keeps that file. The checked output is never discarded for
    it (CodeRabbit 4593970363, #391): it stays in its slot, set aside out of
    every sweep, and the job says where."""
    platform = vol / "romm" / "ps2"
    tools = _Tools(platform)
    tools.install(monkeypatch)
    real_verify = vars(convert_routes.registry.for_mode("createdvd"))["verify"]

    async def squatting_verify(path, *, cancel_event=None):
        (platform / "Game.chd").write_bytes(b"someone else's")
        return await real_verify(path, cancel_event=cancel_event)

    for tool in convert_routes.registry.all():
        monkeypatch.setitem(vars(tool), "verify", squatting_verify)

    job = await _convert(platform / "Game.iso", verify_after=True)

    assert job.status == JobStatus.FAILED
    assert "Game.chd" in (job.error_message or "")
    assert (platform / "Game.chd").read_bytes() == b"someone else's"
    assert (platform / "Game.iso").exists()
    (kept,) = (platform / STAGE_DIR_NAME).glob("park-*/Game.chd")
    assert kept.read_bytes() == b"CHD"
    assert str(kept) in job.error_message


# ---- outside RomM: today's behaviour ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["roms/ps2", "loose"])
async def test_folder_libraries_and_loose_folders_write_in_place_as_before(
    vol, monkeypatch, where,
):
    folder = vol / where
    tools = _Tools(folder)
    tools.install(monkeypatch)

    job = await _convert(folder / "Game.iso", verify_after=True)

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert tools.written_to == [str(folder / "Game.chd")]
    assert tools.verified == [str(folder / "Game.chd")]
    assert not (folder / STAGE_DIR_NAME).exists()


# ---- the shared helper ------------------------------------------------------


def _volume_settings(vol: Path, data_dir: Path, monkeypatch) -> None:
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(vol))
    monkeypatch.setattr(settings, "data_mount_root", str(vol))
    monkeypatch.setattr(settings, "data_dir", str(data_dir))


def test_a_slot_stages_places_and_cleans_up(tmp_path, monkeypatch):
    vol = tmp_path / "vol"
    folder = vol / "ps2"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    slot = staging.open_slot(str(folder))
    try:
        staged = Path(slot.path_for("Game.cue"))
        assert staged.parent.parent == folder / STAGE_DIR_NAME
        staged.write_text("cue")
        staged.with_suffix(".bin").write_bytes(b"bin")
        (staged.parent / ".tool-scratch").write_bytes(b"junk")
        assert partial_writes.recorded_paths() == [str(staged.parent)]

        placed = slot.place(["Game.bin", "Game.cue", "Game.missing"])

        assert placed == [
            (str(staged.with_suffix(".bin")), str(folder / "Game.bin")),
            (str(staged), str(folder / "Game.cue")),
        ]
    finally:
        slot.close()
    assert sorted(os.listdir(folder)) == ["Game.bin", "Game.cue"]
    assert partial_writes.recorded_paths() == []
    slot.close()  # idempotent


def test_a_slot_outside_the_volumes_is_refused(tmp_path, monkeypatch):
    _volume_settings(tmp_path / "vol", tmp_path / "config", monkeypatch)
    (tmp_path / "vol").mkdir()
    (tmp_path / "elsewhere").mkdir()

    with pytest.raises(OSError):
        staging.open_slot(str(tmp_path / "elsewhere"))
    assert not (tmp_path / "elsewhere" / STAGE_DIR_NAME).exists()


def test_a_stage_folder_that_is_a_symlink_is_refused(tmp_path, monkeypatch):
    vol = tmp_path / "vol"
    (vol / "ps2").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (vol / "ps2" / STAGE_DIR_NAME).symlink_to(tmp_path / "outside")
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    with pytest.raises(OSError):
        staging.open_slot(str(vol / "ps2"))
    assert os.listdir(tmp_path / "outside") == []


# ---- a crash, and the startup sweep ----------------------------------------


def _crash_mid_conversion(vol: Path, data_dir: Path, folder: Path) -> Path:
    """A separate process opens a slot, writes half an output and dies
    without cleaning up (``os._exit``): what a SIGKILL leaves."""
    script = (
        "import os, sys\n"
        "from services import staging\n"
        "slot = staging.open_slot(sys.argv[1])\n"
        "open(slot.path_for('Game.chd'), 'wb').write(b'half a chd')\n"
        "print(slot.path, flush=True)\n"
        "os._exit(0)\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(APP_DIR),
        "COMPRESSATORIUM_VOLUMES": str(vol),
        "COMPRESSATORIUM_MOUNT_ROOT": str(vol),
        "CHD_DATA_DIR": str(data_dir),
    }
    done = subprocess.run(
        [sys.executable, "-c", script, str(folder)],
        env=env, capture_output=True, text=True, check=True, timeout=60,
    )
    return Path(done.stdout.strip())


def _age(path: Path, when: float = HOUR_AGO) -> None:
    for entry in (path, *path.iterdir()):
        os.utime(entry, (when, when), follow_symlinks=False)


@pytest.mark.parametrize("folder_name", ["romm/ps2"])
def test_the_startup_sweep_cleans_up_after_a_crash(tmp_path, monkeypatch, folder_name):
    import main

    vol = tmp_path / "vol"
    folder = vol / folder_name
    folder.mkdir(parents=True)
    (folder / "Game.iso").write_bytes(b"iso")
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    slot_path = _crash_mid_conversion(vol, tmp_path / "config", folder)
    assert (slot_path / "Game.chd").exists()
    # Nothing visible to a RomM scan, even before the sweep.
    assert FakeRomm.scan_platform(folder) == ["Game.iso"]
    assert partial_writes.recorded_paths() == [str(slot_path)]

    # The same age rule as a .partial leftover: a young one is kept.
    main.startup_volume_work()
    assert (slot_path / "Game.chd").exists()
    assert partial_writes.recorded_paths() == [str(slot_path)]

    _age(slot_path)
    main.startup_volume_work()

    assert not (folder / STAGE_DIR_NAME).exists()
    assert os.listdir(folder) == ["Game.iso"]
    assert partial_writes.recorded_paths() == []


def test_the_startup_sweep_keeps_a_slot_a_live_job_holds(tmp_path, monkeypatch):
    vol = tmp_path / "vol"
    folder = vol / "ps2"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    slot = staging.open_slot(str(folder))
    try:
        Path(slot.path_for("Game.chd")).write_bytes(b"in progress")
        _age(Path(slot.path))

        assert partial_writes.sweep_recorded() == 0

        assert Path(slot.path_for("Game.chd")).exists()
        assert partial_writes.recorded_paths() == [slot.path]
    finally:
        slot.close()
    assert not (folder / STAGE_DIR_NAME).exists()


def test_opening_a_slot_sweeps_stale_slots_beside_it(tmp_path, monkeypatch):
    """A crashed slot whose journal record is lost goes the next time a job
    stages in that folder, by the same rule."""
    vol = tmp_path / "vol"
    folder = vol / "ps2"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)
    crashed = _crash_mid_conversion(vol, tmp_path / "config", folder)
    for marker in (tmp_path / "config" / "partial-writes").glob("*.path"):
        marker.unlink()
    _age(crashed)

    slot = staging.open_slot(str(folder))
    try:
        assert not crashed.exists()
        assert sorted(os.listdir(folder / STAGE_DIR_NAME)) == sorted(
            [os.path.basename(slot.path), os.path.basename(slot.path) + ".lock"],
        )
    finally:
        slot.close()


# ---- review findings on 50ec2701 --------------------------------------------


def test_placing_never_replaces_a_file_that_appears_after_the_check(tmp_path, monkeypatch):
    """A file another process makes between the existence check and the move
    is never replaced: the placing itself is an atomic no-replace move, and a
    companion already placed goes back into the slot."""
    vol = tmp_path / "vol"
    folder = vol / "ps2"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    slot = staging.open_slot(str(folder))
    try:
        Path(slot.path_for("Game.bin")).write_bytes(b"bin")
        Path(slot.path_for("Game.cue")).write_text("cue")
        # The check sees nothing; the file lands right after it.
        monkeypatch.setattr(staging.StagedOutput, "_exists_in_folder", lambda _s, _n: False)
        (folder / "Game.cue").write_bytes(b"someone else's")

        with pytest.raises(FileExistsError):
            slot.place(["Game.bin", "Game.cue"])

        assert (folder / "Game.cue").read_bytes() == b"someone else's"
        assert not (folder / "Game.bin").exists()
        assert Path(slot.path_for("Game.bin")).read_bytes() == b"bin"
        assert not slot.placed
    finally:
        slot.close()


def test_a_folder_output_is_placed_by_a_no_replace_rename(tmp_path, monkeypatch):
    """A folder can't be hard-linked: it takes the kernel's no-replace rename
    relative to the open folders, and an existing one is refused."""
    from utils import path_utils

    if path_utils._noreplace_renameat_call() is None:
        pytest.skip("no atomic no-replace rename on this platform")
    vol = tmp_path / "vol"
    folder = vol / "xbox"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)

    slot = staging.open_slot(str(folder))
    try:
        Path(slot.path_for("Game")).mkdir()
        Path(slot.path_for("Game"), "default.xbe").write_bytes(b"xbe")
        assert slot.place(["Game"]) == [(slot.path_for("Game"), str(folder / "Game"))]
    finally:
        slot.close()
    assert (folder / "Game" / "default.xbe").read_bytes() == b"xbe"

    slot = staging.open_slot(str(folder))
    try:
        Path(slot.path_for("Game")).mkdir()
        monkeypatch.setattr(staging.StagedOutput, "_exists_in_folder", lambda _s, _n: False)
        with pytest.raises(FileExistsError):
            slot.place(["Game"])
    finally:
        slot.close()
    assert os.listdir(folder / "Game") == ["default.xbe"]


@pytest.mark.asyncio
async def test_a_slot_is_still_closed_when_no_detached_worker_is_free(tmp_path, monkeypatch):
    """Every probe slot taken: the cleanup still runs (on a thread of its
    own), so the slot's descriptors and lock don't stay open until restart."""
    from types import SimpleNamespace

    from services import subprocess_runner

    vol = tmp_path / "vol"
    folder = vol / "ps2"
    folder.mkdir(parents=True)
    _volume_settings(vol, tmp_path / "config", monkeypatch)
    slot = staging.open_slot(str(folder))
    Path(slot.path_for("Game.chd")).write_bytes(b"never placed")
    job = SimpleNamespace(output_path=str(folder / "Game.chd"), mode=ConversionMode.CREATEDVD)

    def no_capacity(_func):
        raise subprocess_runner.ProbeCapacityExceeded("64 probes already blocked")

    monkeypatch.setattr(subprocess_runner, "_probe_in_daemon_thread", no_capacity)

    await convert_routes.job_manager._close_stage(job, slot)

    deadline = time.monotonic() + 5
    while (folder / STAGE_DIR_NAME).exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not (folder / STAGE_DIR_NAME).exists()
    assert partial_writes.recorded_paths() == []


@pytest.mark.asyncio
async def test_a_conversion_into_a_new_subfolder_of_romm_is_staged_there(vol, monkeypatch):
    """The output folder doesn't exist yet: it is made (inside the Volume)
    before the slot, as the tool's runner would have made it."""
    platform = vol / "romm" / "ps2"
    target = platform / "converted"
    tools = _Tools(target)
    tools.install(monkeypatch)

    job = await _convert(platform / "Game.iso", output_dir=str(target))

    assert job.status == JobStatus.COMPLETED, job.error_message
    assert (target / "Game.chd").read_bytes() == b"CHD"
    assert Path(tools.written_to[0]).parent.parent == target / STAGE_DIR_NAME
    assert not (target / STAGE_DIR_NAME).exists()
