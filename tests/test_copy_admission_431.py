"""An Inbox copy is admitted in the same FIFO as conversions (#431).

A copy reserves its ticket when it asks for a slot, as a queued conversion
does, and the dispatcher hands it the local slot when its ticket comes up; it
then takes the cross-process slot the way ``_process_job`` does. Nothing ever
holds the local slot while waiting behind someone else, so with one slot a
copy behind queued conversions, or a copy waiting while a conversion finishes
with more queued, never deadlocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .test_default_compression_345 import _env
from .test_platform_detect import _gamecube

pytestmark = pytest.mark.usefixtures("fresh_detect_cache")
ENV_FIXTURE = _env


@pytest.fixture(name="one_slot")
def _one_slot(env, monkeypatch, tmp_path):
    """MAX_CONCURRENT_JOBS=1, with the
    dispatcher running; each conversion takes a moment."""
    from services.job_manager import job_manager
    from services.tools import registry

    monkeypatch.setattr(job_manager, "max_concurrent", 1)
    monkeypatch.setattr(job_manager, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(job_manager, "_slot_waiters", {})
    service = registry.get("dolphin")._service

    async def slow(cmd, *, input_path, output_path, **kwargs):
        await asyncio.sleep(0.2)
        Path(output_path).write_bytes(b"converted output")
        yield {"progress": 100, "message": "Conversion complete"}

    monkeypatch.setattr(service._runner, "run", slow)
    monkeypatch.setattr(job_manager, "_running", True)
    return job_manager


def _dispatch(job_manager) -> asyncio.Task:
    """The dispatcher, run on the test's own loop."""
    return asyncio.ensure_future(job_manager._dispatcher_loop())


async def _conversions(env, names):
    from models import ConversionMode
    from services.job_manager import job_manager

    out = env["tmp"] / "out"
    out.mkdir(exist_ok=True)
    games = [_gamecube(env["tmp"] / "gc" / f"{name}.iso") for name in names]
    return await job_manager.create_batch_jobs(
        games, ConversionMode.DOLPHIN_RVZ, output_dir=str(out),
    )


async def _copy(job_manager, held: list):
    from models import ConversionMode

    job = job_manager.create_external_job("Game.iso", ConversionMode.INBOX_COPY)
    assert await job_manager.hold_slot(job.id, asyncio.Event())
    held.append(job.id)
    await asyncio.sleep(0.1)  # the copy's file work
    job_manager.release_slot(job.id)
    return job.id


async def _all_done(job_manager, jobs, copy_task):
    from models import JobStatus

    await asyncio.wait_for(copy_task, 10)
    for _ in range(200):
        if all(job_manager.jobs[j.id].status == JobStatus.COMPLETED for j in jobs):
            return
        await asyncio.sleep(0.05)
    raise AssertionError([job_manager.jobs[j.id].status for j in jobs])


@pytest.mark.asyncio
async def test_a_copy_behind_queued_conversions_never_deadlocks(env, one_slot) -> None:
    job_manager = one_slot
    dispatcher = _dispatch(job_manager)
    try:
        jobs = await _conversions(env, ["A", "B"])
        held: list = []

        copy = asyncio.ensure_future(_copy(job_manager, held))
        await _all_done(job_manager, jobs, copy)
        # Every run to its very end (its slot given back) before reading it.
        await job_manager.drain()
    finally:
        dispatcher.cancel()

    assert held
    assert job_manager._semaphore._value == 1


@pytest.mark.asyncio
async def test_a_waiting_copy_gets_the_slot_as_a_conversion_ends_with_more_queued(
    env, one_slot,
) -> None:
    job_manager = one_slot
    dispatcher = _dispatch(job_manager)
    try:
        first = await _conversions(env, ["A"])
        await asyncio.sleep(0.05)  # A is running
        held: list = []
        copy = asyncio.ensure_future(_copy(job_manager, held))
        await asyncio.sleep(0.05)
        more = await _conversions(env, ["B", "C"])

        await _all_done(job_manager, [*first, *more], copy)
        await job_manager.drain()  # every run ended before the slot is read
    finally:
        dispatcher.cancel()

    assert held
    assert job_manager._semaphore._value == 1
