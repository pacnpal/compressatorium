"""`JobManager.shutdown` and `drain` must end when every tracked task is done (#450).

The sets shrink through done callbacks, which only run when the loop gets a
turn. On Python 3.14 awaiting `gather` over finished tasks does not yield, so a
loop that waited on the set itself spun forever. The tests put finished tasks
in the sets with no callback (the worst case) and run under a short timeout, so
a regression fails fast instead of hanging.
"""
from __future__ import annotations

import asyncio
import faulthandler

import pytest
from services.job_manager import JobManager

TIMEOUT = 5


@pytest.fixture(autouse=True)
def _hard_timeout():
    faulthandler.dump_traceback_later(30, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


async def _done_task(fail: bool = False) -> asyncio.Task:
    async def body():
        if fail:
            raise RuntimeError("boom")

    task = asyncio.ensure_future(body())
    await asyncio.wait({task})
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_shutdown_ends_with_done_background_tasks(fail):
    manager = JobManager()
    manager._background_tasks.add(await _done_task(fail))
    await asyncio.wait_for(manager.shutdown(), TIMEOUT)
    assert not manager._background_tasks


@pytest.mark.asyncio
async def test_shutdown_still_awaits_a_task_added_while_waiting():
    manager = JobManager()
    ran = []

    async def late():
        ran.append("late")

    async def early():
        await asyncio.sleep(0.01)
        task = asyncio.ensure_future(late())
        manager._background_tasks.add(task)

    manager._background_tasks.add(asyncio.ensure_future(early()))
    await asyncio.wait_for(manager.shutdown(), TIMEOUT)
    assert ran == ["late"]
    assert not manager._background_tasks


@pytest.mark.asyncio
async def test_drain_ends_with_done_job_and_requeue_tasks():
    manager = JobManager()
    manager._job_tasks.add(await _done_task())
    manager._requeue_tasks.add(await _done_task(fail=True))
    await asyncio.wait_for(manager.drain(), TIMEOUT)
    assert not manager._job_tasks
    assert not manager._requeue_tasks
