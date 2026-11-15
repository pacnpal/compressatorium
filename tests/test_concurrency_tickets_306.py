"""FIFO tickets stay correct across processes and containers (#306).

A ticket file is held under a flock for its holder's lifetime, so the kernel,
not a PID that means nothing in another container, says who is alive: a
holder killed without cleanup (SIGKILL, SIGHUP, the OOM killer) leaves an
unlocked ticket, which the next waiter prunes.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from services.concurrency_manager import ConcurrencyManager

APP = str(Path(__file__).resolve().parents[1] / "app")

HOLDER = textwrap.dedent("""
    import sys, time
    from services.concurrency_manager import ConcurrencyManager
    cm = ConcurrencyManager(1, sys.argv[1])
    cm.reserve_ticket("holder")
    print("ready", flush=True)
    time.sleep(120)
""")


def _start_holder(lock_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": APP}
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_dir)],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    return proc


async def _acquires(cm: ConcurrencyManager, key: str, timeout: float) -> bool:
    try:
        return await asyncio.wait_for(cm.acquire(key), timeout)
    except asyncio.TimeoutError:
        return False


def test_a_killed_holder_does_not_stall_the_next_job(tmp_path):
    lock_dir = tmp_path / "locks"
    server = ConcurrencyManager(1, str(lock_dir))  # already running, like the server
    holder = _start_holder(lock_dir)
    try:
        # Alive and first in line: the server's job waits behind it.
        assert not asyncio.run(_acquires(server, "serverjob", 0.6))
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)

        # Dead without cleanup: its ticket is unlocked, so it is pruned and the
        # job proceeds.
        assert asyncio.run(_acquires(server, "serverjob", 5))
        assert server._list_tickets() == ["serverjob"]
    finally:
        if holder.poll() is None:
            holder.kill()
        server.release("serverjob")


def test_a_live_ticket_is_never_pruned(tmp_path):
    lock_dir = tmp_path / "locks"
    first = ConcurrencyManager(1, str(lock_dir))
    first.reserve_ticket("a")
    second = ConcurrencyManager(1, str(lock_dir))  # construction prunes dead tickets only

    second._prune_dead_tickets()

    assert second._list_tickets() == ["a"]
    first.release_ticket("a")
    assert second._list_tickets() == []


def test_slot_files_are_never_removed(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    slot = lock_dir / "convert_slot_0.lock"
    slot.write_bytes(b"")
    inode = slot.stat().st_ino

    ConcurrencyManager(1, str(lock_dir))

    assert slot.exists() and slot.stat().st_ino == inode


@pytest.mark.parametrize("lazy", [True])
def test_a_lazy_manager_touches_nothing_until_used(tmp_path, lazy):
    lock_dir = tmp_path / "locks"
    cm = ConcurrencyManager(1, str(lock_dir), lazy=lazy)
    assert cm._list_tickets() == []
    assert not lock_dir.exists()
    cm.reserve_ticket("job")
    assert (lock_dir / "queue_counter").exists()
    cm.release_ticket("job")
