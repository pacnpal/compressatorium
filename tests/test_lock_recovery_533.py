"""The maintenance loop is the only queue recovery there is (#533).

The Recover button (``POST /api/jobs/recover``) only ran
``lock_manager.cleanup_stale_locks_periodic``: it removed lock files no
process held. These tests show the automatic path covers what it did. A
lock file left by a process that died never blocks a job, because locks are
``flock``s the kernel drops with their holder. The maintenance tick removes
such files, on its own fixed clock and not a debug setting.
"""
from __future__ import annotations

import asyncio
import fcntl
import os

import pytest
from services import job_manager as job_manager_module
from services.job_manager import JobManager
from services.lock_manager import LockManager


def _manager(lock_dir) -> LockManager:
    manager = LockManager()
    manager._lock_dir = str(lock_dir)
    return manager


def _left_by_a_dead_process(manager: LockManager, path: str) -> str:
    """The lock file a killed holder leaves: on disk, held by nobody."""
    lock_file = manager._lock_file_path(manager._resolve(path))
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "ab") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    # Closed: the kernel let go of the flock, the file stays.
    return lock_file


def test_a_lock_file_left_by_a_dead_process_never_blocks_a_job(tmp_path):
    """The state the Recover button was for: nothing to recover from."""
    out = str(tmp_path / "Game.rvz")
    manager = _manager(tmp_path / "locks")
    _left_by_a_dead_process(manager, out)

    assert manager.acquire_lock(out)
    manager.release_lock(out)


@pytest.mark.asyncio
async def test_the_maintenance_tick_removes_stale_lock_files(tmp_path, monkeypatch):
    """What the button did (``cleanup_stale_locks_periodic``), on every tick.
    A lock a live job holds is left alone."""
    locks = _manager(tmp_path / "locks")
    monkeypatch.setattr(job_manager_module, "lock_manager", locks)
    stale = _left_by_a_dead_process(locks, str(tmp_path / "Old.rvz"))
    held = str(tmp_path / "Live.rvz")
    assert locks.acquire_lock(held)
    live = locks._lock_file_path(locks._resolve(held))

    await JobManager()._handle_background_maintenance()

    assert not os.path.exists(stale)
    # The file itself, not only this manager's map: a removed live file would
    # let another process take the same lock on a new inode.
    assert os.path.exists(live)
    assert locks.is_locked(held)
    locks.release_lock(held)


@pytest.mark.asyncio
async def test_the_maintenance_loop_runs_on_a_fixed_clock(monkeypatch):
    """No debug setting can turn it off: it starts with the dispatcher."""
    monkeypatch.setattr(job_manager_module, "MAINTENANCE_INTERVAL_S", 0.01)
    manager = JobManager()
    ticks = []

    async def tick():
        ticks.append(1)

    monkeypatch.setattr(manager, "_handle_background_maintenance", tick)
    runner = asyncio.ensure_future(manager.process_queue())
    for _ in range(200):
        if len(ticks) >= 2:
            break
        await asyncio.sleep(0.01)
    runner.cancel()
    await manager.shutdown()

    assert len(ticks) >= 2


def test_the_recover_and_stuck_routes_are_gone():
    from main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/jobs/recover" not in paths
    assert "/api/jobs/stuck-status" not in paths
    assert "/api/jobs/history-overflow" not in paths
