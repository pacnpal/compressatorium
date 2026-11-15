"""The delete re-check tells this job's own delete hold from another holder.

From the moment a job decides to delete its Source it holds every delete path
exclusively (``_hold_delete_paths_waiting``, #306), and #295 re-checks those
paths before and after the Referrer hook. The re-check must still refuse a path
another job or process has locked, but never the job's own hold, or no delete
could ever pass.
"""
from __future__ import annotations

import os

import pytest
from services.job_manager import job_manager
from services.lock_manager import LockManager
from utils.delete_plan import build_delete_snapshot


@pytest.fixture(name="source")
def _source(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "chd_volumes", str(tmp_path))
    monkeypatch.setattr(settings, "data_mount_root", str(tmp_path))
    path = tmp_path / "Game.iso"
    path.write_bytes(b"the disc" * 64)
    plan = build_delete_snapshot(str(path))
    assert plan["paths"] == [str(path)], plan
    return str(path), plan["fingerprints"], str(tmp_path / "Game.rvz")


@pytest.mark.asyncio
async def test_a_delete_path_another_manager_holds_keeps_the_source(source):
    path, fingerprints, output = source
    other = LockManager()  # another process's lock, in the shared lock directory
    assert other.acquire_lock(path, allow_existing=True)
    try:
        with pytest.raises(RuntimeError, match="locked by an active conversion"):
            await job_manager._check_delete_paths("job-a", [path], fingerprints, output)
    finally:
        other.release_lock(path)
    assert os.path.exists(path)


@pytest.mark.asyncio
async def test_this_jobs_own_delete_hold_does_not_block_the_delete(source):
    path, fingerprints, output = source
    assert job_manager._try_hold_for_delete("job-b", [path])
    try:
        await job_manager._check_delete_paths("job-b", [path], fingerprints, output)
    finally:
        job_manager._release_delete_holds("job-b")


@pytest.mark.asyncio
async def test_another_jobs_delete_hold_still_blocks(source):
    path, fingerprints, output = source
    assert job_manager._try_hold_for_delete("job-c", [path])
    try:
        with pytest.raises(RuntimeError, match="locked by an active conversion"):
            await job_manager._check_delete_paths("job-d", [path], fingerprints, output)
    finally:
        job_manager._release_delete_holds("job-c")
