"""The job stream learns of every job, however short (#456).

``/api/jobs/events`` used to follow only the jobs its 0.1 s loop saw queued
or running, so a job that started and ended between two ticks (a patch job
lives about 60 ms) never reached an open stream. The stream now follows each
job from the moment the job manager registers it.
"""

import asyncio
import json
from contextlib import suppress

import pytest

from app.models import ConversionMode
from app.routes import convert as convert_routes
from app.services.job_manager import JobManager

_TERMINAL = ("complete", "error", "cancelled")


@pytest.fixture(name="manager")
def _manager(monkeypatch):
    manager = JobManager(max_concurrent=1, max_job_history=50)
    monkeypatch.setattr(convert_routes, "job_manager", manager)
    return manager


class _Reader:
    """Drive the stream the way the server does, without pause, collecting
    every live event (snapshot and history frames left out)."""

    def __init__(self, stream):
        self.stream = stream
        self.seen = []
        self.opened = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async for event in self.stream:
            if event.get("event") == "history":
                self.opened.set()
            if event.get("event") in ("snapshot", "history"):
                continue
            data = json.loads(event["data"])
            self.seen.append((event["event"], data["job_id"], data))

    async def until_terminal(self, job_id, timeout=5):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if any(
                seen_id == job_id and name in _TERMINAL
                for name, seen_id, _ in self.seen
            ):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"no terminal event for {job_id}: {self.seen}")

    async def close(self):
        self.task.cancel()
        with suppress(BaseException):
            await self.task
        with suppress(Exception):
            await self.stream.aclose()


async def _open_stream():
    """Open the stream and let it send its opening frames, so its loop is
    parked in a tick when the caller acts."""
    response = await convert_routes.job_events()
    reader = _Reader(response.body_iterator)
    await asyncio.wait_for(reader.opened.wait(), timeout=3)
    return reader


def _events_for(seen, job_id):
    return [name for name, seen_id, _ in seen if seen_id == job_id]


@pytest.mark.asyncio
async def test_a_job_that_starts_and_ends_between_ticks_reaches_the_stream(manager):
    reader = await _open_stream()
    try:
        # Created and finished with no await between that lets the stream's
        # loop run: it never sees this job queued or running.
        short = manager.create_external_job("Patch", ConversionMode.PATCH)
        await manager.finish_external_job(short.id, success=True)
        await reader.until_terminal(short.id)
        # A second job after it: by its end, anything more for the first
        # would have arrived.
        marker = manager.create_external_job("Marker", ConversionMode.PATCH)
        await asyncio.sleep(0.3)
        await manager.finish_external_job(marker.id, success=False, error_message="x")
        await reader.until_terminal(marker.id)
    finally:
        await reader.close()

    assert _events_for(reader.seen, short.id) == ["complete"]
    complete = next(data for _, job_id, data in reader.seen if job_id == short.id)
    assert complete["job"]["status"] == "completed"
    assert _events_for(reader.seen, marker.id) == ["error"]


@pytest.mark.asyncio
async def test_six_short_jobs_all_reach_the_stream(manager):
    """The smoke repro: six patch jobs, each one over between two ticks."""
    reader = await _open_stream()
    ids = []
    try:
        for i in range(6):
            job = manager.create_external_job(f"Patch{i}", ConversionMode.PATCH)
            await manager.update_external_job(job.id, progress=50, message="Patching")
            await manager.finish_external_job(job.id, success=True)
            ids.append(job.id)
            await asyncio.sleep(0.3)
        for job_id in ids:
            await reader.until_terminal(job_id)
    finally:
        await reader.close()

    for job_id in ids:
        assert _events_for(reader.seen, job_id) == ["progress", "complete"], job_id


@pytest.mark.asyncio
async def test_a_job_created_on_a_worker_thread_reaches_the_stream(manager):
    reader = await _open_stream()
    try:
        job = await asyncio.to_thread(
            manager.create_external_job, "Threaded", ConversionMode.PATCH,
        )
        await manager.finish_external_job(job.id, success=True)
        await reader.until_terminal(job.id)
    finally:
        await reader.close()

    assert _events_for(reader.seen, job.id) == ["complete"]


@pytest.mark.asyncio
async def test_a_long_job_streams_its_progress_then_completes_once(manager):
    reader = await _open_stream()
    try:
        job = manager.create_external_job("Long", ConversionMode.PATCH)
        for step in (10, 40, 70):
            await manager.update_external_job(job.id, progress=step)
            # Long enough for the loop's own scan to see the job running,
            # which must not follow it a second time.
            await asyncio.sleep(0.25)
        await manager.finish_external_job(job.id, success=True)
        await reader.until_terminal(job.id)
        marker = manager.create_external_job("Marker", ConversionMode.PATCH)
        await asyncio.sleep(0.3)
        await manager.finish_external_job(marker.id, success=True)
        await reader.until_terminal(marker.id)
    finally:
        await reader.close()

    assert _events_for(reader.seen, job.id) == [
        "progress", "progress", "progress", "complete",
    ]
    progress = [data["progress"] for _, job_id, data in reader.seen if job_id == job.id]
    assert progress == [10, 40, 70, 100]


@pytest.mark.asyncio
async def test_a_job_running_before_the_stream_opened_still_streams_once(manager):
    running = manager.create_external_job("Before", ConversionMode.PATCH)
    reader = await _open_stream()
    try:
        await asyncio.sleep(0.25)
        await manager.update_external_job(running.id, progress=30)
        await asyncio.sleep(0.25)
        await manager.finish_external_job(running.id, success=True)
        await reader.until_terminal(running.id)
    finally:
        await reader.close()

    assert _events_for(reader.seen, running.id) == ["progress", "complete"]


@pytest.mark.asyncio
async def test_a_burst_of_events_keeps_its_end_state(manager):
    """More events than a follower's queue holds, faster than one a tick:
    the stream drains each queue every tick, so the end state still lands."""
    reader = await _open_stream()
    try:
        job = manager.create_external_job("Burst", ConversionMode.PATCH)
        for step in range(90):
            await manager.update_external_job(job.id, progress=step)
        await asyncio.sleep(0.15)
        for step in range(90):
            await manager.update_external_job(job.id, progress=step)
        await manager.finish_external_job(job.id, success=True)
        await reader.until_terminal(job.id)
    finally:
        await reader.close()

    assert _events_for(reader.seen, job.id).count("complete") == 1


@pytest.mark.asyncio
async def test_a_closed_stream_stops_following_new_jobs(manager):
    reader = await _open_stream()
    await reader.close()

    job = manager.create_external_job("After", ConversionMode.PATCH)
    await manager.finish_external_job(job.id, success=True)

    assert not manager._subscribers.get(job.id)
