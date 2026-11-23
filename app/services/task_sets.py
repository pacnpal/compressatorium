"""Waiting for a module's set of background tasks to end (#579).

A service that starts work as tasks keeps them in a module-level set, and each
task removes itself in a done callback (``task.add_done_callback(tasks.discard)``).
Its ``drain()``, which tests, the CLI and shutdown await, waits here.
"""

from __future__ import annotations

import asyncio


async def drain_tasks(tasks: set[asyncio.Task]) -> None:
    """Wait until *tasks* is empty: every task in it has ended, and so has
    every task added to it during the wait.

    The finished tasks are removed here instead of waiting for their done
    callbacks, which only run on a later loop turn. Since Python 3.12, awaiting
    ``gather`` over tasks that have all finished completes without yielding. A
    loop that waited for the callbacks to empty the set would never give the
    event loop that turn, and would spin forever on a task that ended just
    before it woke up (#450, #579). Each task's exception is collected
    (``return_exceptions``), so none is logged as never retrieved.
    """
    while tasks:
        snapshot = list(tasks)
        await asyncio.gather(*snapshot, return_exceptions=True)
        tasks.difference_update(snapshot)  # every one has ended
