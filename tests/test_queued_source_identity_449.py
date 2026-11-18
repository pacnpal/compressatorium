"""A conversion re-checks its Source's identity and containment when it runs (#449).

Every conversion is queued carrying the identity of the Source it saw
(``utils.path_utils.queued_source_identity``). Here a parent folder is swapped
for a symlink out of the Volumes after the job is queued, by a Rule sweep and
by a hand-started conversion: the job is refused, and the tool never runs, so
nothing outside the Volumes is read.
"""
from __future__ import annotations

import asyncio
import errno
import os
from datetime import datetime, timezone

import pytest

from .test_cli_run_rules_307 import (  # the fixtures, registered by import
    RVZ_RULE,
    _dolphin,  # noqa: F401
    _env,  # noqa: F401
    _iso,
    _store_rules,
)


def _swap_for_link(folder, outside) -> None:
    """Move *folder* aside and put a link to *outside* in its place, holding a
    file of every name the folder had."""
    outside.mkdir()
    for entry in folder.iterdir():
        (outside / entry.name).write_bytes(b"outside the Volumes")
    folder.rename(folder.with_name(folder.name + ".moved"))
    folder.symlink_to(outside, target_is_directory=True)


def test_a_rule_sweep_job_refuses_a_folder_swapped_for_a_link(env, dolphin, tmp_path):
    from models import JobStatus
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager
    from services.rules import sweep as rules_sweep

    iso = _iso(env)
    _store_rules(env, {"shelf": RVZ_RULE})

    async def run():
        await rules_sweep.sweep(env["shelf"], platform_ids=["gc"])
        [job] = [j for j in job_manager.jobs.values() if j.file_path == str(iso)]
        _swap_for_link(iso.parent, tmp_path / "outside")
        try:
            await job_manager._process_job(job.id)
        finally:
            concurrency_manager.release(job.id)
        return job

    job = asyncio.run(run())

    assert job.status == JobStatus.FAILED
    assert "no longer inside the configured Volumes" in job.error_message
    assert dolphin["runs"] == []  # the tool never read anything


@pytest.mark.asyncio
async def test_a_hand_started_job_refuses_a_folder_swapped_for_a_link(e2e_env, tmp_path_factory):
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    folder = e2e_env["tmp_path"] / "games"
    folder.mkdir()
    iso = folder / "Game.iso"
    iso.write_bytes(b"inside the Volume")
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(iso), mode=ConversionMode.CSO_COMPRESS,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))

    _swap_for_link(folder, tmp_path_factory.mktemp("elsewhere") / "outside")
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.FAILED.value
    assert "no longer inside the configured Volumes" in job.error_message
    assert e2e_env["calls"] == []  # the tool never read anything


@pytest.mark.asyncio
async def test_a_job_queued_without_an_identity_is_refused(tmp_volume, tmp_path):
    """A file job that reached the queue some other way carries no identity:
    nothing tells what it would read from what it was queued for."""
    from app.models import ConversionJob, ConversionMode, JobStatus
    from app.services.job_manager import job_manager

    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc")
    job = ConversionJob(
        id="noidentity449", file_path=str(iso), filename=iso.name,
        mode=ConversionMode.CSO_COMPRESS, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(tmp_path / "Game.cso"),
    )
    job_manager.jobs[job.id] = job
    try:
        await job_manager._process_job(job.id)
    finally:
        job_manager.jobs.pop(job.id, None)

    assert job.status == JobStatus.FAILED
    assert "queued without its Source's identity" in job.error_message
    assert not os.path.exists(job.output_path)


@pytest.mark.asyncio
async def test_a_sheet_submitted_through_a_link_reads_its_tracks_beside_the_link(e2e_env):
    """codex 4614857083: the job reads the sheet by the path it was submitted
    as, so its tracks are the ones beside that path (the ones it locked), not
    those beside the link's target."""
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    root = e2e_env["tmp_path"]
    (root / "sheets").mkdir()
    (root / "selected").mkdir()
    sheet = root / "sheets" / "Game.cue"
    sheet.write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    (root / "sheets" / "Game.bin").write_bytes(b"\1" * 2352)
    (root / "selected" / "Game.bin").write_bytes(b"\2" * 2352)
    submitted = root / "selected" / "Game.cue"
    submitted.symlink_to(sheet)

    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(submitted), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.COMPLETED.value, job.error_message
    assert [call["input"] for call in e2e_env["calls"]] == [str(submitted)]


@pytest.mark.asyncio
async def test_a_link_re_pointed_after_queueing_is_refused(e2e_env):
    """The submitted path must still lead to the file checked: a sheet link
    re-pointed to another in-Volume file is refused, never followed."""
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    root = e2e_env["tmp_path"]
    first, second = root / "a.iso", root / "b.iso"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    link = root / "Game.iso"
    link.symlink_to(first)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(link), mode=ConversionMode.CSO_COMPRESS,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    link.unlink()
    link.symlink_to(second)
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.FAILED.value
    assert "leads somewhere else" in job.error_message
    assert e2e_env["calls"] == []


@pytest.mark.asyncio
async def test_a_batch_bigger_than_the_bound_keeps_every_identity(monkeypatch, tmp_path):
    """codex 4614857109: the bound restarts after each Source, so batch size
    alone never turns a healthy Source's identity into an error."""
    import time

    from app.models import ConversionMode
    from app.services import job_manager as jm

    monkeypatch.setattr(jm, "_CANONICAL_PROBE_SECONDS", 0.2)

    def slow(path, companions=()):
        time.sleep(0.05)
        return {"path": path}

    monkeypatch.setattr(jm, "queued_source_identity", slow)
    specs = [{"file_path": str(tmp_path / f"{i}.iso")} for i in range(10)]  # 0.5s in all
    await jm.job_manager._capture_source_identities(specs, ConversionMode.CSO_COMPRESS)

    assert [spec["source_identity"] for spec in specs] == [
        {"path": spec["file_path"]} for spec in specs
    ]


@pytest.mark.asyncio
async def test_a_source_that_stops_answering_keeps_the_identities_before_it(
    monkeypatch, tmp_path,
):
    import threading

    from app.models import ConversionMode
    from app.services import job_manager as jm

    monkeypatch.setattr(jm, "_CANONICAL_PROBE_SECONDS", 0.2)
    release = threading.Event()

    def stuck_on_the_third(path, companions=()):
        if path.endswith("2.iso"):
            release.wait(5)
        return {"path": path}

    monkeypatch.setattr(jm, "queued_source_identity", stuck_on_the_third)
    specs = [{"file_path": str(tmp_path / f"{i}.iso")} for i in range(5)]
    try:
        await jm.job_manager._capture_source_identities(specs, ConversionMode.CSO_COMPRESS)
    finally:
        release.set()

    got = [spec["source_identity"] for spec in specs]
    assert got[:2] == [{"path": specs[0]["file_path"]}, {"path": specs[1]["file_path"]}]
    assert all("no answer" in identity["error"] for identity in got[2:])
    # Never reached, so no verdict on the Image: a Rule's failed-hold skips
    # it and the next sweep queues it again (CodeRabbit 4615955365).
    from types import SimpleNamespace

    from services.rules import sweep

    assert [sweep._never_checked(SimpleNamespace(_source_identity=i)) for i in got] == [
        False, False, True, True, True,
    ]
    assert not sweep._never_checked(SimpleNamespace(_source_identity={"error": "a link"}))


CUE = 'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'


@pytest.mark.asyncio
async def test_a_track_replaced_after_queueing_is_refused(e2e_env):
    """CodeRabbit 4614956755: the tracks (and split parts) the converter opens
    itself are captured with the Source and checked again when it runs."""
    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    root = e2e_env["tmp_path"]
    cue = root / "Game.cue"
    cue.write_text(CUE)
    track = root / "Game.bin"
    track.write_bytes(b"\1" * 2352)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(cue), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    track.unlink()
    track.write_bytes(b"\2" * 4704)  # another disc's data under the same name
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.FAILED.value
    assert "Game.bin, a companion file it reads with the Source, changed" in job.error_message
    assert e2e_env["calls"] == []


@pytest.mark.asyncio
async def test_a_sheet_swapped_out_of_the_volume_is_never_read(
    e2e_env, tmp_path_factory, monkeypatch,
):
    """CodeRabbit 4614956778 / 4614956871: a queued sheet whose folder now
    leads out of the Volumes isn't parsed for tracks to lock before the
    identity check refuses it; nothing outside is read."""
    from services import job_manager as jm  # the module the route queues with

    from app.models import ConversionMode, DuplicateAction, JobCreateRequest, JobStatus
    from app.routes import convert as convert_routes

    folder = e2e_env["tmp_path"] / "games"
    folder.mkdir()
    (folder / "Game.cue").write_text(CUE)
    (folder / "Game.bin").write_bytes(b"\1" * 2352)
    job = await convert_routes.create_job(JobCreateRequest(
        file_path=str(folder / "Game.cue"), mode=ConversionMode.CREATECD,
        duplicate_action=DuplicateAction.SKIP, delete_on_verify=False,
    ))
    outside = tmp_path_factory.mktemp("elsewhere") / "outside"
    _swap_for_link(folder, outside)
    (outside / "Game.cue").write_text(CUE)

    sheets_read = []
    real_plan = jm.build_delete_plan

    def spying_plan(path, *args, **kwargs):
        sheets_read.append(path)
        return real_plan(path, *args, **kwargs)

    monkeypatch.setattr(jm, "build_delete_plan", spying_plan)
    # The companion read too (CodeRabbit 4615955508).
    import utils.delete_plan as delete_plan

    real_tracks = delete_plan.sheet_track_paths

    def spying_tracks(path, *args, **kwargs):
        sheets_read.append(path)
        return real_tracks(path, *args, **kwargs)

    monkeypatch.setattr(delete_plan, "sheet_track_paths", spying_tracks)
    await convert_routes.job_manager._process_job(job.id)

    assert job.status.value == JobStatus.FAILED.value
    assert "no longer inside the configured Volumes" in job.error_message
    assert sheets_read == []
    assert e2e_env["calls"] == []


@pytest.mark.asyncio
async def test_a_refused_submission_captures_again_when_retried(tmp_volume, tmp_path, monkeypatch):
    """CodeRabbit 4614956744: identities are captured into the attempt's own
    copies, never written back into the caller's specs."""
    from app.models import ConversionMode
    from app.services.job_manager import QueueBackpressureError, job_manager

    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"disc")
    specs = [{"file_path": str(iso), "output_dir": None}]

    def full(_count):
        raise QueueBackpressureError(1, 1, 1)

    monkeypatch.setattr(job_manager, "_enforce_queue_backpressure_locked", full)
    with pytest.raises(QueueBackpressureError):
        await job_manager.create_jobs_atomic(specs, ConversionMode.CSO_COMPRESS)
    assert "source_identity" not in specs[0]


@pytest.mark.asyncio
async def test_a_sheet_job_with_no_identity_never_reads_its_sheet(
    tmp_volume, tmp_path, monkeypatch,
):
    """CodeRabbit 4615215842 / 4615217022: a CUE job with no usable identity
    locks only its Source and is refused; its sheet is never parsed."""
    from models import ConversionJob, ConversionMode, JobStatus
    from services import job_manager as jm

    cue = tmp_path / "Game.cue"
    cue.write_text(CUE)
    (tmp_path / "Game.bin").write_bytes(b"\1" * 2352)
    sheets_read = []
    real_plan = jm.build_delete_plan

    def spying_plan(path, *args, **kwargs):
        sheets_read.append(path)
        return real_plan(path, *args, **kwargs)

    monkeypatch.setattr(jm, "build_delete_plan", spying_plan)
    for job_id, identity in (("noid449", None), ("errid449", {"error": "no answer"})):
        job = ConversionJob(
            id=job_id, file_path=str(cue), filename=cue.name,
            mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc), output_path=str(tmp_path / "Game.chd"),
        )
        job._source_identity = identity
        assert jm.job_manager._input_paths(job) == [str(cue)]
        jm.job_manager.jobs[job.id] = job
        try:
            await jm.job_manager._process_job(job.id)
        finally:
            jm.job_manager.jobs.pop(job.id, None)
        assert job.status == JobStatus.FAILED
        assert job.error_message.startswith("Not converted:")
    assert sheets_read == []


def test_a_sheet_linked_across_volumes_keeps_its_companions(tmp_path, monkeypatch):
    """codex 4615261287: each companion is captured and checked under its own
    Volume root. A sheet in one Volume linked from another names tracks beside
    the link, in the other Volume."""
    from utils.path_utils import queued_source_identity, queued_source_problem

    from config import settings

    vol_a, vol_b = tmp_path / "volA", tmp_path / "volB"
    vol_a.mkdir()
    vol_b.mkdir()
    monkeypatch.setattr(settings, "chd_volumes", f"{vol_a},{vol_b}")
    (vol_b / "Game.cue").write_text(CUE)
    submitted = vol_a / "Game.cue"
    submitted.symlink_to(vol_b / "Game.cue")
    track = vol_a / "Game.bin"
    track.write_bytes(b"\1" * 2352)

    identity = queued_source_identity(str(submitted), "createcd")
    [companion] = identity["companions"]
    assert (companion["path"], companion["root"]) == (str(track), str(vol_a))
    assert queued_source_problem(str(submitted), identity, "createcd") is None

    track.unlink()
    track.write_bytes(b"\2" * 4704)
    assert "changed" in queued_source_problem(str(submitted), identity, "createcd")


def test_companions_are_the_selected_modes_only(tmp_volume, tmp_path):
    """codex 4615553372: the identity's companions come from the mode's own
    tool (the shared ``source_companion_paths``), so a ``createhd`` on
    ``game_part1.wud`` captures no part 2 and a changed part 2 doesn't refuse
    it, while a Wii U job on the same file checks part 2."""
    from utils.path_utils import queued_source_identity, queued_source_problem

    part1, part2 = tmp_path / "game_part1.wud", tmp_path / "game_part2.wud"
    part1.write_bytes(b"\1" * 64)
    part2.write_bytes(b"\2" * 64)
    raw = queued_source_identity(str(part1), "createhd")
    wiiu = queued_source_identity(str(part1), "jwud_compress")
    assert raw["companions"] == []
    assert [each["path"] for each in wiiu["companions"]] == [str(part2)]

    part2.unlink()
    part2.write_bytes(b"\3" * 128)
    assert queued_source_problem(str(part1), raw, "createhd") is None
    assert "changed" in queued_source_problem(str(part1), wiiu, "jwud_compress")


def test_track_links_that_swap_targets_are_refused(tmp_volume, tmp_path):
    """codex 4615553385: companions are compared reference by reference, in
    the sheet's order, so two track links swapping targets (the same files,
    read in another order) refuse the job."""
    from utils.path_utils import queued_source_identity, queued_source_problem

    cue = tmp_path / "Game.cue"
    cue.write_text(
        'FILE "A.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
        'FILE "B.bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
    )
    data_x, data_y = tmp_path / "x.raw", tmp_path / "y.raw"
    data_x.write_bytes(b"\1" * 2352)
    data_y.write_bytes(b"\2" * 2352)
    link_a, link_b = tmp_path / "A.bin", tmp_path / "B.bin"
    link_a.symlink_to(data_x)
    link_b.symlink_to(data_y)
    identity = queued_source_identity(str(cue), "createcd")
    assert [each["path"] for each in identity["companions"]] == [str(data_x), str(data_y)]
    assert queued_source_problem(str(cue), identity, "createcd") is None

    link_a.unlink()
    link_b.unlink()
    link_a.symlink_to(data_y)
    link_b.symlink_to(data_x)
    assert "aren't the ones it was queued with" in queued_source_problem(
        str(cue), identity, "createcd",
    )


def test_input_locks_come_from_the_captured_companions(tmp_volume, tmp_path, monkeypatch):
    """CodeRabbit 4615955374: for an in-place job, the input locks are the
    Source and the companions captured at queue time; nothing is read inside
    the unbounded lock wait."""
    from models import ConversionJob, ConversionMode, JobStatus
    from services import job_manager as jm
    from utils.path_utils import queued_source_identity

    cue = tmp_path / "Game.cue"
    cue.write_text(CUE)
    track = tmp_path / "Game.bin"
    track.write_bytes(b"\1" * 2352)
    job = ConversionJob(
        id="locks449", file_path=str(cue), filename=cue.name,
        mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(tmp_path / "Game.chd"),
    )
    job._source_identity = queued_source_identity(job.file_path, job.mode.value)

    def no_reads(*_args, **_kwargs):
        raise AssertionError("the Source was read inside the input-lock wait")

    monkeypatch.setattr(jm, "build_delete_plan", no_reads)
    monkeypatch.setattr(jm, "queued_source_problem", no_reads)
    assert jm.job_manager._input_paths(job) == [str(cue), str(track)]


def test_a_track_named_with_a_double_colon_is_a_literal_file(tmp_volume, tmp_path):
    """CodeRabbit 4615955480: a companion is a literal path, never archive
    syntax, so a sheet naming `Track::01.bin` is captured as that file."""
    from utils.path_utils import queued_source_identity, queued_source_problem

    cue = tmp_path / "Game.cue"
    cue.write_text('FILE "Track::01.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    track = tmp_path / "Track::01.bin"
    track.write_bytes(b"\1" * 2352)
    identity = queued_source_identity(str(cue), "createcd")
    assert [each["path"] for each in identity["companions"]] == [str(track)]
    assert queued_source_problem(str(cue), identity, "createcd") is None


def test_a_folder_named_with_a_double_colon_is_a_literal_folder(tmp_volume, tmp_path):
    """CodeRabbit 4616898142: the resolved Source path is literal when its
    Volume root is chosen, so a folder named `a::b` is no archive."""
    from utils.path_utils import queued_source_identity, queued_source_problem

    folder = tmp_path / "a::b"
    folder.mkdir()
    (folder / "Game.iso").write_bytes(b"disc")
    # Submitted through a link (a submitted `::` names an archive member).
    (tmp_path / "games").symlink_to(folder, target_is_directory=True)
    iso = tmp_path / "games" / "Game.iso"
    identity = queued_source_identity(str(iso), "cso_compress")
    assert identity["path"] == str(folder / "Game.iso")
    assert queued_source_problem(str(iso), identity, "cso_compress") is None


def _in_place_job(tmp_path, job_id):
    from models import ConversionJob, ConversionMode, JobStatus
    from utils.path_utils import queued_source_identity

    cue = tmp_path / "Game.cue"
    cue.write_text(CUE)
    (tmp_path / "Game.bin").write_bytes(b"\1" * 2352)
    job = ConversionJob(
        id=job_id, file_path=str(cue), filename=cue.name,
        mode=ConversionMode.CREATECD, status=JobStatus.QUEUED,
        created_at=datetime.now(timezone.utc), output_path=str(tmp_path / "Game.chd"),
    )
    job._source_identity = queued_source_identity(job.file_path, job.mode.value)
    return job


@pytest.mark.asyncio
async def test_a_source_check_that_times_out_is_no_verdict(tmp_volume, tmp_path, monkeypatch):
    """Review 5960207187: the job's own Source check timing out refuses the
    job but marks it unanswered, so a Rule's failed-hold skips it, like a
    stalled capture."""
    import time
    from types import SimpleNamespace

    from services import job_manager as jm
    from services.rules import sweep

    job = _in_place_job(tmp_path, "timeout449")
    monkeypatch.setattr(jm, "_CANONICAL_PROBE_SECONDS", 0.1)
    monkeypatch.setattr(jm, "queued_source_problem", lambda *_a, **_k: time.sleep(1))

    problem = await jm.job_manager._source_identity_problem_bounded(job)

    assert "couldn't be checked" in problem
    assert sweep._never_checked(SimpleNamespace(_source_identity=job._source_identity))


@pytest.mark.asyncio
async def test_a_cancel_during_the_source_check_ends_the_job_cancelled(
    tmp_volume, tmp_path, monkeypatch,
):
    """Review 5960207187: a Cancel while the Source check waits is the job's
    cancel, never a refusal."""
    import time

    from services import job_manager as jm
    from services.subprocess_runner import ConversionCancelled

    job = _in_place_job(tmp_path, "cancel449")
    cancel = asyncio.Event()

    def slow(*_args, **_kwargs):
        time.sleep(1)

    monkeypatch.setattr(jm, "queued_source_problem", slow)
    asyncio.get_running_loop().call_later(0.05, cancel.set)
    with pytest.raises(ConversionCancelled):
        await jm.job_manager._source_identity_problem_bounded(job, cancel)


def test_the_active_job_inventory_uses_the_captured_companions(tmp_volume, tmp_path, monkeypatch):
    """Review 5960207187: another sweep asking which files queued jobs use
    gets the captured companions, without the sheet being read again."""
    from services import job_manager as jm

    job = _in_place_job(tmp_path, "inventory449")

    def no_reads(*_args, **_kwargs):
        raise AssertionError("the sheet was read for the active-job inventory")

    monkeypatch.setattr(jm, "build_delete_plan", no_reads)
    paths = jm.job_manager._candidate_paths(job)
    assert paths[:2] == [str(tmp_path / "Game.cue"), str(tmp_path / "Game.bin")]



@pytest.mark.asyncio
async def test_a_volume_root_that_doesnt_answer_is_no_verdict(tmp_volume, tmp_path, monkeypatch):
    """CodeRabbit 4617787310: a Volume-root probe that times out is "couldn't
    tell", never "outside the Volumes": at capture and at the job's own check
    the identity is marked unanswered, so a Rule's failed-hold skips the
    unchanged Source (``sweep._never_checked``)."""
    from types import SimpleNamespace

    from models import ConversionMode
    from services import job_manager as jm
    from services.rules import sweep
    from utils import path_utils

    job = _in_place_job(tmp_path, "rootprobe449")
    monkeypatch.setattr(path_utils, "_fresh_root", lambda _configured: None)  # no answer

    problem = await jm.job_manager._source_identity_problem_bounded(job)
    assert "couldn't be checked" in problem and "didn't answer" in problem
    assert sweep._never_checked(SimpleNamespace(_source_identity=job._source_identity))

    specs = [{"file_path": job.file_path}]
    await jm.job_manager._capture_source_identities(specs, ConversionMode.CREATECD)
    assert specs[0]["source_identity"].get("unanswered") is True
    assert sweep._never_checked(SimpleNamespace(_source_identity=specs[0]["source_identity"]))


@pytest.mark.parametrize("silent_call", [1, 2], ids=["source-root", "companion-root"])
def test_a_later_root_probe_that_doesnt_answer_is_no_verdict(
    tmp_volume, tmp_path, monkeypatch, silent_call,
):
    """CodeRabbit 4617888601: the first containment probe answers, then the
    Volume-root lookup for the Source (call 1) or its companion (call 2)
    doesn't. Capture marks the identity unanswered, never a plain refusal,
    so ``sweep._never_checked`` is True."""
    from types import SimpleNamespace

    from models import ConversionMode
    from services import job_manager as jm
    from services.rules import sweep
    from utils import path_utils

    (tmp_path / "Game.cue").write_text(CUE)
    (tmp_path / "Game.bin").write_bytes(b"\\1" * 2352)
    real_root, real_fresh = path_utils.lock_boundary_root, path_utils._fresh_root
    calls = []

    def lock_boundary_root(path, **kwargs):
        calls.append(path)
        if len(calls) == silent_call:  # this root probe gets no answer
            monkeypatch.setattr(path_utils, "_fresh_root", lambda _configured: None)
        try:
            return real_root(path, **kwargs)
        finally:
            monkeypatch.setattr(path_utils, "_fresh_root", real_fresh)

    monkeypatch.setattr(path_utils, "lock_boundary_root", lock_boundary_root)
    specs = [{"file_path": str(tmp_path / "Game.cue")}]
    asyncio.run(jm.job_manager._capture_source_identities(specs, ConversionMode.CREATECD))

    identity = specs[0]["source_identity"]
    assert len(calls) == silent_call
    assert identity.get("unanswered") is True and "didn't answer" in identity["error"]
    assert sweep._never_checked(SimpleNamespace(_source_identity=identity))


def test_a_sheet_whose_tracks_cant_be_confined_now_is_no_verdict(tmp_volume, tmp_path, monkeypatch):
    """The same at the job's own check: the sheet's tracks are confined by
    root probes too, and one that doesn't answer is "couldn't tell", not
    "the companion files aren't the ones it was queued with"."""
    from utils import path_utils
    from utils.path_utils import queued_source_identity, queued_source_problem

    cue = tmp_path / "Game.cue"
    cue.write_text(CUE)
    (tmp_path / "Game.bin").write_bytes(b"\\1" * 2352)
    identity = queued_source_identity(str(cue), "createcd")
    import utils.delete_plan as delete_plan

    real_sheet = delete_plan.sheet_track_paths

    def silent_sheet(path, *args, **kwargs):
        path_utils._note_unknown()  # a track's root probe got no answer
        return []

    monkeypatch.setattr(delete_plan, "sheet_track_paths", silent_sheet)
    with pytest.raises(TimeoutError, match="didn't answer"):
        queued_source_problem(str(cue), identity, "createcd")
    monkeypatch.setattr(delete_plan, "sheet_track_paths", real_sheet)
    assert queued_source_problem(str(cue), identity, "createcd") is None


def _timing_out_on(monkeypatch, name):
    """``stat_at`` raising TimeoutError (ETIMEDOUT) for *name* alone."""
    from utils import path_utils

    real_stat = path_utils.stat_at

    def stat_at(entry, folder):
        if entry == name:
            raise TimeoutError(errno.ETIMEDOUT, "Connection timed out", entry)
        return real_stat(entry, folder)

    monkeypatch.setattr(path_utils, "stat_at", stat_at)


@pytest.mark.asyncio
async def test_a_companion_whose_stat_times_out_is_no_verdict(tmp_volume, tmp_path, monkeypatch):
    """codex 4618766182: a companion's stat that times out (ETIMEDOUT, a
    TimeoutError) is "couldn't tell", never "the companion changed": at
    capture and at the job's own check the identity is unanswered."""
    from types import SimpleNamespace

    from models import ConversionMode
    from services import job_manager as jm
    from services.rules import sweep

    job = _in_place_job(tmp_path, "stattimeout449")  # captured while it answers
    _timing_out_on(monkeypatch, "Game.bin")

    problem = await jm.job_manager._source_identity_problem_bounded(job)
    assert "couldn't be checked" in problem and "changed" not in problem
    assert sweep._never_checked(SimpleNamespace(_source_identity=job._source_identity))

    specs = [{"file_path": job.file_path}]
    await jm.job_manager._capture_source_identities(specs, ConversionMode.CREATECD)
    assert specs[0]["source_identity"].get("unanswered") is True
    assert sweep._never_checked(SimpleNamespace(_source_identity=specs[0]["source_identity"]))


@pytest.mark.asyncio
async def test_a_source_or_sheet_read_that_times_out_is_no_verdict(
    tmp_volume, tmp_path, monkeypatch,
):
    """The same for the Source's own folder walk and for the sheet's read
    (its track list would come back empty, read as "other companions")."""
    from types import SimpleNamespace

    from services import job_manager as jm
    from services.rules import sweep
    from utils import path_utils, sheet_text

    job = _in_place_job(tmp_path, "srctimeout449")
    real_folder = path_utils._folder_beneath_root

    def folder_times_out(path, root):
        raise TimeoutError(errno.ETIMEDOUT, "Connection timed out", path)

    monkeypatch.setattr(path_utils, "_folder_beneath_root", folder_times_out)
    problem = await jm.job_manager._source_identity_problem_bounded(job)
    assert "couldn't be checked" in problem
    assert sweep._never_checked(SimpleNamespace(_source_identity=job._source_identity))
    monkeypatch.setattr(path_utils, "_folder_beneath_root", real_folder)

    job = _in_place_job(tmp_path, "sheettimeout449")

    def sheet_times_out(*_args, **_kwargs):
        raise TimeoutError(errno.ETIMEDOUT, "Connection timed out")

    monkeypatch.setattr(sheet_text, "open", sheet_times_out, raising=False)
    problem = await jm.job_manager._source_identity_problem_bounded(job)
    assert "couldn't be checked" in problem
    assert sweep._never_checked(SimpleNamespace(_source_identity=job._source_identity))
