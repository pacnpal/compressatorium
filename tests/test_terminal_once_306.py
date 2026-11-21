"""Terminal listeners hear about each job exactly once (#306, from #307).

A queued job cancelled before it starts went through both the cancel path
and the start path's terminal path, so every terminal listener (SSE,
notifications, the Ledger's counters, check results) heard it twice. The
job manager now announces a job's end once, whichever path gets there first.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from models import ConversionJob, ConversionMode, JobStatus
from services.job_manager import job_manager
from services.lock_manager import lock_manager


@pytest.fixture(name="heard")
def _heard():
    heard: list[tuple[str, str]] = []

    def listener(job):
        heard.append((job.id, job.status.value))

    job_manager.add_terminal_listener(listener)
    yield heard
    job_manager.remove_terminal_listener(listener)


def _job(tmp_path, job_id, *, mode=ConversionMode.DOLPHIN_RVZ):
    source = tmp_path / f"{job_id}.iso"
    source.write_bytes(b"disc")
    job = ConversionJob(
        id=job_id, file_path=str(source), filename=source.name, mode=mode,
        status=JobStatus.QUEUED, created_at=datetime.now(timezone.utc),
        output_path=str(tmp_path / f"{job_id}.rvz"),
    )
    job_manager.jobs[job.id] = job
    return job


def _cleanup(job):
    lock_manager.release_lock(job.output_path)
    job_manager.jobs.pop(job.id, None)
    job_manager._cancel_events.pop(job.id, None)
    job_manager._cancelled.discard(job.id)
    job_manager._release_inputs(job.id)


@pytest.mark.asyncio
async def test_a_queued_job_cancelled_while_it_starts_is_announced_once(
    tmp_path, monkeypatch, heard,
):
    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    monkeypatch.setattr(lock_manager, "_ready", False)
    job = _job(tmp_path, "racecancel")
    try:
        # The cancel lands on the queued job...
        assert await job_manager.cancel_job(job.id)
        # ...while the start path, already under way, ends it its own way.
        await job_manager._fail_before_start(
            job.id, job, "Could not lock the Source: boom", dir_lock_held=False,
        )
        await job_manager._process_job(job.id)
    finally:
        _cleanup(job)

    assert [entry for entry in heard if entry[0] == job.id] == [(job.id, "cancelled")]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "lock_error"])
async def test_every_terminal_path_announces_once(
    tmp_path, tmp_volume, monkeypatch, heard, outcome,
):
    import asyncio

    from services import job_manager as job_manager_module
    from utils.path_utils import queued_source_identity

    monkeypatch.setattr(lock_manager, "_lock_dir", str(tmp_path / "locks"))
    monkeypatch.setattr(lock_manager, "_ready", False)
    job = _job(tmp_path, f"once{outcome}")
    job._source_identity = queued_source_identity(job.file_path, job.mode.value)
    tool = job_manager_module.registry.for_mode("dolphin_rvz")

    async def converting(*_args, **_kwargs):
        if outcome == "failed":
            raise RuntimeError("converter broke")
        if outcome == "cancelled":
            await job_manager.cancel_job(job.id)
            await asyncio.sleep(0)
            raise job_manager_module.ConversionCancelled("Conversion cancelled")
        with open(job.output_path, "wb") as fh:
            fh.write(b"rvz")
        yield {"progress": 100, "message": "done"}

    monkeypatch.setattr(tool, "convert", converting)
    if outcome == "lock_error":
        def raising(path):
            raise OSError("lock dir unavailable")
        monkeypatch.setattr(lock_manager, "acquire_input_lock", raising)
    try:
        await job_manager._process_job(job.id)
    finally:
        _cleanup(job)

    expected = {"completed": "completed", "failed": "failed",
                "cancelled": "cancelled", "lock_error": "failed"}[outcome]
    assert [entry for entry in heard if entry[0] == job.id] == [(job.id, expected)]
