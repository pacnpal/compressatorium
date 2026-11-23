"""Every module's ``drain()`` ends once its tasks have, without spinning (#579).

Each tracked task leaves its module's ``_tasks`` set through a done callback,
which only runs on a later loop turn. Since Python 3.12, awaiting ``gather`` over
tasks that are all finished completes at once without yielding. So a
``while _tasks: await gather(*_tasks)`` loop that wakes up to find a task that
has finished but is still in the set never gives the loop the turn that removes
it, and spins forever at full CPU. Intake's ``drain()`` hung
``test_a_converted_image_from_an_archive_stays_placed_across_unpacks`` this way
(#579), as ``JobManager`` once did (#450).

The interleaving is built exactly, with no sleeps. The drained task starts a
second task and ends. The second task ends one loop turn later, in the same
turn that resumes ``drain()`` but scheduled ahead of it, so its done callback
is queued behind ``drain()``. A spinning ``drain()`` never yields, so
``asyncio.wait_for`` can't bound it. Each case runs on its own loop in a
thread joined with a deadline, and a spin fails loudly instead of hanging the
run.
"""
from __future__ import annotations

import asyncio
import importlib
import threading

import pytest

# Ample for a loop that ends in a few turns; a spin never ends at all.
BOUND_SECONDS = 10

DRAINS = [
    "services.inbox.intake",
    "services.inbox.trigger",
    "services.rules.check",
    "services.patch_bench",
    "services.duplicates",
    "services.duplicate_cleanup",
    "services.dat_rename",
]


def _bounded(scenario) -> None:
    """Run *scenario()* on a fresh loop in a daemon thread; fail if it is
    still running after :data:`BOUND_SECONDS`."""
    errors: list[BaseException] = []

    def main() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test's thread
            errors.append(exc)

    thread = threading.Thread(target=main, daemon=True, name="drain-579")
    thread.start()
    thread.join(BOUND_SECONDS)
    if thread.is_alive():
        pytest.fail(
            f"drain() was still running after {BOUND_SECONDS}s: it is spinning on a "
            "finished task its done callback hasn't removed yet (#579)",
        )
    if errors:
        raise errors[0]


def _tracked(tasks: set, work) -> asyncio.Task:
    """Start *work* as a task tracked the way each module tracks its own."""
    task = asyncio.ensure_future(work)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


@pytest.mark.parametrize("module_name", DRAINS)
def test_drain_ends_when_a_task_ends_in_the_turn_that_resumes_it(module_name):
    module = importlib.import_module(module_name)
    tasks: set = module._tasks
    ended: list[str] = []

    async def second() -> None:
        await asyncio.sleep(0)  # one loop turn, then it ends
        ended.append("second")

    async def first() -> None:
        # Started by a task being drained (an intake copy that starts the
        # move to processed/, an unpack that takes its Images on).
        _tracked(tasks, second())
        ended.append("first")

    async def scenario() -> None:
        _tracked(tasks, first())
        await module.drain()

    try:
        _bounded(scenario)
    finally:
        tasks.clear()
    assert ended == ["first", "second"]


@pytest.mark.parametrize("module_name", DRAINS)
def test_drain_still_waits_for_a_task_that_is_running(module_name):
    """Dropping finished tasks never drops one still running."""
    module = importlib.import_module(module_name)
    tasks: set = module._tasks
    ended: list[str] = []

    async def slow() -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        ended.append("slow")

    async def failing() -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        _tracked(tasks, failing())
        _tracked(tasks, slow())
        await module.drain()
        assert not tasks

    try:
        _bounded(scenario)
    finally:
        tasks.clear()
    assert ended == ["slow"]
