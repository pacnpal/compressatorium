"""The concurrency invariant counts only jobs that take a conversion slot (#377).

A check run, a metadata scan and a DAT match are external jobs:
``create_external_job`` makes them PROCESSING from the start, and they never
enter the queue, so they never hold one of the ``max_concurrent`` slots. The
invariant in ``_process_job`` counted every PROCESSING job, so a conversion
starting beside a check run logged a false ERROR. It now counts by the same
predicate the dispatcher admits by (``_is_conversion_job``).
"""
from __future__ import annotations

import logging

import pytest

from .test_default_compression_345 import _env
from .test_platform_detect import _gamecube

pytestmark = pytest.mark.usefixtures("fresh_detect_cache")
# #345's "env" fixture (a Volume, an empty queue, nothing saved, the
# dolphin-tool command captured at its runner), shared here.
ENV_FIXTURE = _env
_INVARIANT = "concurrency invariant violated"


async def _queue_rvz(env, name: str):
    from app.models import ConversionMode, JobCreateRequest
    from app.routes import convert as convert_routes

    iso = _gamecube(env["tmp"] / "gc" / f"{name}.iso")
    return await convert_routes.create_job(JobCreateRequest(
        file_path=iso, mode=ConversionMode.DOLPHIN_RVZ,
    ))


@pytest.mark.asyncio
async def test_a_conversion_beside_a_check_run_logs_no_invariant_error(
    env, monkeypatch, caplog,
) -> None:
    from app.models import ConversionMode, JobStatus
    from services.job_manager import job_manager

    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    check = job_manager.create_external_job("Check run", ConversionMode.CHECK)
    job = await _queue_rvz(env, "Melee")

    with caplog.at_level(logging.ERROR):
        await job_manager._process_job(job.id)

    assert check.status == JobStatus.PROCESSING
    assert job_manager.jobs[job.id].status == JobStatus.COMPLETED, job.error_message
    assert not [r for r in caplog.records if _INVARIANT in r.getMessage()]


@pytest.mark.asyncio
async def test_two_conversions_over_the_limit_still_log_the_invariant(
    env, monkeypatch, caplog,
) -> None:
    from app.models import JobStatus
    from services.concurrency_manager import concurrency_manager
    from services.job_manager import job_manager

    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    first = await _queue_rvz(env, "Melee")
    second = await _queue_rvz(env, "Pikmin")
    # Forced: the dispatcher's semaphore would never admit both at once. The
    # first gives up its queue ticket so the second's turn comes.
    job_manager.jobs[first.id].status = JobStatus.PROCESSING
    concurrency_manager.release_ticket(first.id)

    with caplog.at_level(logging.ERROR):
        await job_manager._process_job(second.id)

    errors = [r.getMessage() for r in caplog.records if _INVARIANT in r.getMessage()]
    assert errors == [
        "Processing concurrency invariant violated: processing=2 max_concurrent=1",
    ]


@pytest.mark.asyncio
async def test_the_dispatcher_admits_only_conversions(env, monkeypatch) -> None:
    """Admission and the invariant share one rule: an external job that
    somehow reached the queue takes no slot and is not run; a conversion is."""
    import asyncio

    from app.models import ConversionMode
    from services.job_manager import job_manager

    check = job_manager.create_external_job("Check run", ConversionMode.CHECK)
    job = await _queue_rvz(env, "Melee")
    ran: list[str] = []

    async def run_job(job_id):
        ran.append(job_id)
        job_manager._semaphore.release()

    monkeypatch.setattr(job_manager, "_run_job", run_job)
    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(job_manager, "_running", True)
    job_manager._queue.put_nowait((0, check.id))
    dispatcher = asyncio.create_task(job_manager._dispatcher_loop())
    await asyncio.wait_for(job_manager._queue.join(), timeout=5)
    dispatcher.cancel()

    assert ran == [job.id]
