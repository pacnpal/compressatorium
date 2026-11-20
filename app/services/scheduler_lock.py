"""One process works a data directory at a time (#307, #533).

The server, ``compressatorium run-rules`` and ``compressatorium convert`` each
run jobs, sweep Rules and write the Volumes. Only one of them may do that on a
data directory at once, so each holds this lock while it works: the server from
startup to shutdown, ``run-rules`` for its pass, ``convert`` while its jobs
run in-process. That is what keeps a CLI and a server from ever working on
one file, and what keeps ``MAX_CONCURRENT_JOBS`` one limit for the directory.

``convert`` never runs beside a server: when the server holds the lock, the
CLI hands its jobs to it over HTTP (``services.server_client``), found from
the host and port the server names here. ``run-rules`` is turned away when the
lock is held (exit 3). A ``convert`` waits for another CLI. The server waits
for whoever holds it (``acquire_waiting``), logging who, so a server started
during a nightly pass, or beside another server, starts when the lock is free
instead of crash-looping under a restart policy.

The lock is an exclusive ``flock`` on ``scheduler.lock`` in the shared lock
directory (``<data dir>/locks`` by default, see ``config._default_lock_dir``),
the same directory as the job locks (#306). Every
container that mounts the same ``/config`` sees it, and the kernel drops it when
its holder dies, however it dies. The file is never unlinked, so the lock is
always on the file every process opens. While held it names its holder (role,
PID, host, start time, and a server's port) so the process that is turned away
can say why, and a CLI can find the server.

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
# Present while a server waits for the lock (`acquire_waiting`). uvicorn opens
# its port only once startup returns, so the Docker HEALTHCHECK reads this
# instead. Named per host (a container's hostname): containers that share
# /config must not answer for each other.
WAITING_FILE = "scheduler.waiting"

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


def waiting_path() -> str:
    return os.path.join(lock_dir(), f"{WAITING_FILE}.{socket.gethostname()}")


def _mark_waiting(holder: dict) -> None:
    try:
        Path(waiting_path()).write_text(json.dumps(holder), encoding="utf-8")
    except OSError:
        pass  # the marker only keeps the health check green


def _unmark_waiting() -> None:
    with contextlib.suppress(OSError):
        os.remove(waiting_path())


class SchedulerLockHeld(Exception):
    """Another process is the scheduler for this data directory."""

    def __init__(self, holder: dict | None) -> None:
        self.holder = holder or {}
        super().__init__(self.describe())

    def describe(self) -> str:
        by = self.holder.get("by")
        who = {
            "server": "the server",
            "run-rules": "a run-rules pass",
            "convert": "a compressatorium convert",
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
            "only one process works a data directory at a time"
        )


class SchedulerLock:
    """A held scheduler lock. :meth:`release` lets it go."""

    def __init__(self, fd: int, record: dict | None = None) -> None:
        self._fd: int | None = fd
        self._record = record or {}

    def note_port(self, port: int | None) -> None:
        """Name the port this server answers on, once, for a CLI to find it."""
        if self._fd is None or not port or self._record.get("port") == port:
            return
        self._record = {**self._record, "port": port}
        _write_record(self._fd, self._record)

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


def _write_record(fd: int, record: dict) -> None:
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode("utf-8"), 0)
    except OSError:
        pass  # the holder note is for messages only; the flock is the lock


def _held_by(fd: int, by: str) -> SchedulerLock:
    """Write the holder note into the file this fd holds, and wrap it."""
    record = {
        "by": by,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    _write_record(fd, record)
    return SchedulerLock(fd, record)


def acquire(by: str, *, wait: float = _ACQUIRE_WAIT) -> SchedulerLock:
    """Take the lock for ``by`` (``"server"``, ``"run-rules"``, ``"convert"``), or raise.

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

    While it waits, ``waiting_path()`` exists and names the holder; it is
    removed on every way out, and a stale one (a waiter killed outright) is
    removed by the next call on this host.
    """
    fd = _open()
    told_at: float | None = None
    try:
        while not _try(fd):
            if should_stop():
                raise StoppedWaiting("Stopped while waiting for the Rule scheduler lock")
            now = time.monotonic()
            if told_at is None or now - told_at >= _WAIT_LOG_EVERY:
                holder = _read_holder(fd)
                _mark_waiting(holder)
                on_wait(SchedulerLockHeld(holder))
                told_at = now
            await asyncio.sleep(_WAIT_POLL)
    except BaseException:
        os.close(fd)
        raise
    finally:
        _unmark_waiting()
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
