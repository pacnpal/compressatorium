"""A job queue whose lock directory was deleted while in use makes it again.

Found while reviewing #307 (codex 4576665771): ``ConcurrencyManager`` made its
directory once, on first use. Deleted afterwards (a tmp cleaner on the
``$TMPDIR/chd-locks`` fallback, someone clearing ``/config/locks``), every
holder, ticket and slot file failed to open and the next job waited forever,
logging a warning every 30 s.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat

from services.concurrency_manager import ConcurrencyManager


def test_a_queue_whose_lock_dir_was_deleted_still_runs_the_next_job(tmp_path):
    lock_dir = tmp_path / "locks"
    queue = ConcurrencyManager(1, str(lock_dir))

    async def scenario():
        assert await asyncio.wait_for(queue.acquire("first"), 5)
        queue.release("first")
        shutil.rmtree(lock_dir)
        assert await asyncio.wait_for(queue.acquire("second"), 5), "the job got its slot"
        queue.release("second")

    asyncio.run(scenario())

    assert lock_dir.is_dir()
    assert stat.S_IMODE(os.stat(lock_dir).st_mode) == 0o700
    holders = [p.name for p in lock_dir.iterdir() if p.name.startswith("holder_")]
    assert holders == [f"holder_{queue._holder_id}.lock"]
