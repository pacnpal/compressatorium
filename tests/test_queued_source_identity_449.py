"""A conversion re-checks its Source's identity and containment when it runs (#449).

Every conversion is queued carrying the identity of the Source it saw
(``utils.path_utils.queued_source_identity``). Here a parent folder is swapped
for a symlink out of the Volumes after the job is queued, by a Rule sweep and
by a hand-started conversion: the job is refused, and the tool never runs, so
nothing outside the Volumes is read.
"""
from __future__ import annotations

import asyncio
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
