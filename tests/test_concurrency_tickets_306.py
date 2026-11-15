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


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_a_thousand_queued_jobs_keep_fd_use_flat(tmp_path):
    """Liveness is one held lock per process, not one open file per job: a
    Select-All of a few thousand files must not exhaust the fd limit."""
    cm = ConcurrencyManager(1, str(tmp_path / "locks"))
    cm.reserve_ticket("first")
    before = _open_fds()

    for i in range(1000):
        cm.reserve_ticket(f"job{i}")

    assert _open_fds() - before <= 1
    assert len(cm._list_tickets()) == 1001
    for i in range(1000):
        cm.release_ticket(f"job{i}")
    assert cm._list_tickets() == ["first"]


def test_a_queue_that_cannot_be_listed_is_not_read_as_empty(tmp_path, monkeypatch):
    """EMFILE from listdir used to mean "nobody is queued", so no job ever
    reached the front and the queue deadlocked; now the waiter retries."""
    import errno

    from services import concurrency_manager as cm_module

    cm = ConcurrencyManager(1, str(tmp_path / "locks"))
    real_listdir = os.listdir
    failures = {"left": 3}

    def flaky_listdir(path):
        if path == cm.lock_dir and failures["left"]:
            failures["left"] -= 1
            raise OSError(errno.EMFILE, "Too many open files")
        return real_listdir(path)

    monkeypatch.setattr(cm_module.os, "listdir", flaky_listdir)
    warnings = []
    monkeypatch.setattr(
        cm_module.logger, "warning", lambda msg, *args: warnings.append(msg % args),
    )
    with pytest.raises(OSError):
        cm._list_tickets()

    assert asyncio.run(_acquires(cm, "job", 5))
    assert failures["left"] == 0
    assert any("could not be read" in w for w in warnings)
    cm.release("job")


def test_orphaned_temp_files_of_dead_writers_are_swept(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    orphan = lock_dir / "queue_4_2_job.ticket.12345.tmp"
    orphan.write_text("{}")

    ConcurrencyManager(1, str(lock_dir))

    assert not orphan.exists()


def test_a_hung_up_holder_does_not_stall_the_next_job(tmp_path):
    lock_dir = tmp_path / "locks"
    server = ConcurrencyManager(1, str(lock_dir))
    holder = _start_holder(lock_dir)
    try:
        holder.send_signal(signal.SIGHUP)
        holder.wait(timeout=10)
        assert asyncio.run(_acquires(server, "serverjob", 5))
    finally:
        if holder.poll() is None:
            holder.kill()
        server.release("serverjob")
