"""A detached call's own TimeoutError is not its deadline (codex 4579904147).

``asyncio.TimeoutError`` is the built-in ``TimeoutError``, which a network
filesystem raises as ``ETIMEDOUT``. The bounded detached helpers tell the two
apart by who finished: a call that settled with its own error has that error
re-raised; only a wait that ran out while the call still ran is the deadline
(``DeadlineExpired``, or ``MutationInFlight`` for a mutation).
"""
from __future__ import annotations

import asyncio
import errno
import time

import pytest
from services.subprocess_runner import (
    DeadlineExpired,
    MutationInFlight,
    run_detached_mutation,
    run_detached_undoable,
)


def _etimedout(*_args, **_kwargs):
    raise TimeoutError(errno.ETIMEDOUT, "Operation timed out")


def _slow(*_args, **_kwargs):
    time.sleep(1.0)
    return "late"


def test_a_mutations_own_timeout_error_is_its_failure_not_in_flight():
    handed = []

    async def attempt():
        await run_detached_mutation(
            _etimedout, "path", timeout=5,
            hand_off=lambda: handed.append(True) or (lambda: None),
        )

    with pytest.raises(TimeoutError) as caught:
        asyncio.run(attempt())
    assert not isinstance(caught.value, MutationInFlight)
    assert caught.value.errno == errno.ETIMEDOUT
    assert handed == []


def test_a_mutation_still_running_at_the_deadline_is_in_flight():
    async def attempt():
        await run_detached_mutation(
            _slow, timeout=0.1, hand_off=lambda: (lambda: None),
        )

    with pytest.raises(MutationInFlight):
        asyncio.run(attempt())
    time.sleep(1.0)


def test_an_undoable_calls_own_timeout_error_is_not_the_deadline():
    undone = []

    async def attempt():
        await run_detached_undoable(_etimedout, timeout=5, undo=undone.append)

    with pytest.raises(TimeoutError) as caught:
        asyncio.run(attempt())
    assert not isinstance(caught.value, DeadlineExpired)
    assert caught.value.errno == errno.ETIMEDOUT
    assert undone == []


def test_an_undoable_call_past_its_deadline_raises_deadline_expired():
    async def attempt():
        await run_detached_undoable(_slow, timeout=0.1, undo=lambda _r: None)

    with pytest.raises(DeadlineExpired):
        asyncio.run(attempt())
    time.sleep(1.0)


def test_a_bounded_probes_own_timeout_error_is_not_the_deadline():
    from services.subprocess_runner import run_detached_bounded

    with pytest.raises(TimeoutError) as caught:
        asyncio.run(run_detached_bounded(_etimedout, timeout=5))
    assert not isinstance(caught.value, DeadlineExpired)
    assert caught.value.errno == errno.ETIMEDOUT

    with pytest.raises(DeadlineExpired):
        asyncio.run(run_detached_bounded(_slow, timeout=0.1))
    time.sleep(1.0)


def test_a_stall_bounded_reads_own_timeout_error_propagates(monkeypatch):
    """Regression: the stall bound now tells them apart the same way."""
    from services import subprocess_runner

    monkeypatch.setattr(subprocess_runner, "conversion_stall_bound", lambda: 5)
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(subprocess_runner.run_detached_within_stall_bound(
            _etimedout, what="Reading",
        ))
    assert caught.value.errno == errno.ETIMEDOUT


def _fails_at_the_deadline(seconds):
    """Raises its own error just as a wait of *seconds* expires."""
    def call(*_args, **_kwargs):
        time.sleep(seconds)
        raise ValueError("the call's own failure")
    return call


@pytest.mark.parametrize("helper", ["bounded", "undoable", "mutation", "stall"])
def test_a_failure_that_lands_at_the_deadline_is_that_failure(helper, monkeypatch):
    """CodeRabbit 4580102968: the worker settles with its own error just as
    the wait runs out; every helper re-raises that error, not a timeout."""
    from services import subprocess_runner

    seconds = 0.2
    func = _fails_at_the_deadline(seconds)
    # The wait's deadline lands just after the worker failed: the snapshot
    # sees a settled worker with an error, and the wait's TimeoutError.
    original_wait_for = asyncio.wait_for

    async def late_wait_for(awaitable, timeout):
        future = asyncio.ensure_future(awaitable)
        await asyncio.sleep(seconds + 0.2)
        future.cancel()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(subprocess_runner.asyncio, "wait_for", late_wait_for)

    async def attempt():
        if helper == "bounded":
            return await subprocess_runner.run_detached_bounded(func, timeout=seconds)
        if helper == "undoable":
            return await subprocess_runner.run_detached_undoable(
                func, timeout=seconds, undo=lambda _r: None,
            )
        if helper == "mutation":
            return await subprocess_runner.run_detached_mutation(
                func, timeout=seconds, hand_off=lambda: (lambda: None),
            )
        monkeypatch.setattr(subprocess_runner, "conversion_stall_bound", lambda: 5)
        return await subprocess_runner.run_detached_within_stall_bound(func, what="x")

    try:
        with pytest.raises(ValueError, match="own failure"):
            asyncio.run(attempt())
    finally:
        monkeypatch.setattr(subprocess_runner.asyncio, "wait_for", original_wait_for)
