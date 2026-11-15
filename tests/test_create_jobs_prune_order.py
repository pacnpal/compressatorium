"""Queueing never awaits after it registers a job (CodeRabbit 4576895540).

``create_jobs_atomic`` / ``create_job`` used to prune history *after*
registering their jobs. The prune awaits (``delete_job`` cleans temp dirs), so
a cancellation there ended the call with jobs registered and queued but never
returned: the caller could not withdraw what it did not know it had. The
prune now runs first; it only removes finished jobs, so the claim check that
follows answers the same. A cancelled prune registers nothing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import ConversionMode
from app.services.job_manager import JobManager


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False], ids=["create_jobs_atomic", "create_job"])
async def test_a_cancellation_in_the_prune_leaves_no_job_registered(tmp_path, monkeypatch, batch):
    mgr = JobManager(max_concurrent=1, max_job_history=10)

    async def cancelled_prune(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(mgr, "_prune_jobs", cancelled_prune)
    source, output = str(tmp_path / "a.iso"), str(tmp_path / "a.chd")

    with pytest.raises(asyncio.CancelledError):
        if batch:
            await mgr.create_jobs_atomic(
                [{"file_path": source, "output_path": output}], ConversionMode.CREATEDVD,
            )
        else:
            await mgr.create_job(source, ConversionMode.CREATEDVD, output_path=output)

    assert mgr.jobs == {}
    assert mgr._queue.empty()


@pytest.mark.asyncio
async def test_the_prune_runs_before_any_job_is_registered(tmp_path, monkeypatch):
    mgr = JobManager(max_concurrent=1, max_job_history=10)
    seen: list[int] = []

    async def recording_prune(**_kwargs):
        seen.append(len(mgr.jobs))

    monkeypatch.setattr(mgr, "_prune_jobs", recording_prune)

    jobs = await mgr.create_jobs_atomic(
        [{"file_path": str(tmp_path / "a.iso"), "output_path": str(tmp_path / "a.chd")}],
        ConversionMode.CREATEDVD,
    )

    assert seen == [0]
    assert [job.id for job in jobs] == list(mgr.jobs)
