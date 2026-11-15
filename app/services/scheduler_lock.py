"""One Rule scheduler per data directory (#307).

The server sweeps the stored Rules on its own clock, and ``compressatorium
run-rules`` sweeps them from cron. Two of them on one data directory would
plan against the same Rules and Volumes at once, so each holds this lock while
it runs: the server from startup to shutdown, ``run-rules`` for its pass.
``run-rules`` is turned away when the lock is held (exit 3). The server waits
for it (``acquire_waiting``), logging who holds it, so a server started during
a nightly pass, or beside another server, starts when the lock is free instead
of crash-looping under a restart policy.

The lock is an exclusive ``flock`` on ``scheduler.lock`` in the shared lock
directory (``<data dir>/locks`` by default, see ``config._default_lock_dir``),
the same directory and the same kind of lock as the job queue's (#306). Every
container that mounts the same ``/config`` sees it, and the kernel drops it when
its holder dies, however it dies. The file is never unlinked, so the lock is
always on the file every process opens. While held it names its holder (role,
PID, host, start time) so the process that is turned away can say why.

``CHD_CONCURRENCY_LOCK_DIR`` moves this lock with the rest; processes that
share a data directory must share the lock directory too.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from config import settings

LOCK_FILE = "scheduler.lock"

# How long `acquire` retries. A dry run's probe holds the lock shared for an
# instant; a real start must not be refused over that.
_ACQUIRE_WAIT = 1.0
_POLL = 0.05
# `acquire_waiting`: how often it tries, and how often it says who it waits for.
_WAIT_POLL = 0.5
_WAIT_LOG_EVERY = 30.0


def lock_dir() -> str:
    return settings.concurrency_lock_dir or str(Path(settings.data_dir) / "locks")


def lock_path() -> str:
    return os.path.join(lock_dir(), LOCK_FILE)


class SchedulerLockHeld(Exception):
    """Another process is the scheduler for this data directory."""

    def __init__(self, holder: dict | None) -> None:
        self.holder = holder or {}
        super().__init__(self.describe())

    def describe(self) -> str:
        by = self.holder.get("by")
        who = {
            "server": "the server",
            "run-rules": "another run-rules",
        }.get(by, "another process")
        details = []
        if self.holder.get("pid"):
            where = f" on {self.holder['host']}" if self.holder.get("host") else ""
            details.append(f"pid {self.holder['pid']}{where}")
        if self.holder.get("since"):
            details.append(f"since {self.holder['since']}")
        extra = f" ({', '.join(details)})" if details else ""
        return (
            f"the data directory {settings.data_dir} is in use by {who}{extra}; "
            "only one Rule scheduler runs per data directory"
        )


class SchedulerLock:
    """A held scheduler lock. :meth:`release` lets it go."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_holder(fd: int) -> dict:
    try:
        raw = os.pread(fd, 4096, 0)
        holder = json.loads(raw.decode("utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return holder if isinstance(holder, dict) else {}


def _open() -> int:
    os.makedirs(lock_dir(), mode=0o700, exist_ok=True)
    return os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)


def _try(fd: int) -> bool:
    """One non-blocking try. True when this fd now holds the lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _held_by(fd: int, by: str) -> SchedulerLock:
    """Write the holder note into the file this fd holds, and wrap it."""
    try:
        record = json.dumps({
            "by": by,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, record, 0)
    except OSError:
        pass  # the holder note is for messages only; the flock is the lock
    return SchedulerLock(fd)


def acquire(by: str, *, wait: float = _ACQUIRE_WAIT) -> SchedulerLock:
    """Take the lock for ``by`` (``"server"``, ``"run-rules"``), or raise.

    Never blocks for long: it retries for ``wait`` seconds and then raises
    :class:`SchedulerLockHeld`, naming the holder. For ``run-rules``, which
    is turned away; the server uses :func:`acquire_waiting`.
    """
    fd = _open()
    deadline = time.monotonic() + max(0.0, wait)
    try:
        while not _try(fd):
            if time.monotonic() >= deadline:
                raise SchedulerLockHeld(_read_holder(fd))
            time.sleep(_POLL)
    except BaseException:
        os.close(fd)
        raise
    return _held_by(fd, by)


class StoppedWaiting(Exception):
    """``acquire_waiting`` gave up because ``should_stop`` said so."""


async def acquire_waiting(
    by: str,
    *,
    on_wait: Callable[[SchedulerLockHeld], None],
    should_stop: Callable[[], bool] = lambda: False,
) -> SchedulerLock:
    """Take the lock for ``by``, waiting as long as another process holds it.

    Polls a non-blocking flock with ``asyncio.sleep`` in between, so the event
    loop stays free and a cancelled caller stops at once. ``on_wait`` is told
    who holds it on the first miss and then every ``_WAIT_LOG_EVERY`` seconds.
    Raises :class:`StoppedWaiting` once ``should_stop()`` is true (a SIGTERM
    during startup). The file is closed on every way out but success.
    """
    fd = _open()
    told_at: float | None = None
    try:
        while not _try(fd):
            if should_stop():
                raise StoppedWaiting("Stopped while waiting for the Rule scheduler lock")
            now = time.monotonic()
            if told_at is None or now - told_at >= _WAIT_LOG_EVERY:
                on_wait(SchedulerLockHeld(_read_holder(fd)))
                told_at = now
            await asyncio.sleep(_WAIT_POLL)
    except BaseException:
        os.close(fd)
        raise
    return _held_by(fd, by)


def check() -> None:
    """Raise :class:`SchedulerLockHeld` when another process holds the lock.

    For a dry run: it creates nothing and keeps nothing. A missing lock file
    means no process holds it.
    """
    try:
        fd = os.open(lock_path(), os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SchedulerLockHeld(_read_holder(fd)) from None
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
