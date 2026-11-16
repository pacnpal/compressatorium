"""The hidden temp files ``archive_edit.rewrite_zip`` writes, and sweeping
the ones a hard kill left behind (#331).

A rewrite writes to ``.partial-XXXXXXXX.tmp`` beside its target and places it
only once it is checked (``.<target name>.partial-XXXXXXXX.tmp`` before #331).
A process killed hard (SIGKILL, power loss) leaves that file: hidden, ending in
``.tmp`` so library scanners skip it, but taking space forever.

* **One name pattern.** :func:`is_leftover_name` matches exactly what the
  writer names its temp file, eight characters from ``mkstemp``'s alphabet,
  in either spelling. Its lock file is the same name plus ``.lock``. Nothing
  else is ever swept.
* **A live write holds a sidecar lock, never a lock on the archive.** The
  writer (:func:`claim_temp`) creates ``<temp>.lock`` and takes an exclusive
  ``flock`` on it *before* the temp file exists, and keeps it until the temp
  file is placed or removed, in this process or any other (the CLI). The temp
  archive itself is never locked, so a check that reads it works where locks
  are mandatory (SMB). The lock file is opened for reading and writing, which
  an exclusive ``flock`` needs on NFS.
* **The sweep takes the same lock.** It removes a leftover only when it can
  take the sidecar's lock (the writer is gone), the leftover is older than
  ``MIN_AGE_SECONDS``, it opens with ``O_NOFOLLOW`` as a regular file, and the
  path still names that inode; then it removes the sidecar too. A sidecar left
  without its temp file is swept the same way.
* **Fail closed.** The folder must be inside a configured Volume
  (``is_within_configured_volumes``, which answers "outside" when it can't
  tell). Anything uncertain is kept.
* **Two times to sweep.** :func:`sweep_folder` runs before every rewrite into
  that folder. :func:`sweep_recorded` runs at startup, from a small journal in
  the data directory (one file per temp file in flight, written when the temp
  file is created and removed when it is gone), so a crash is cleaned up
  without walking every Volume.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from logging_setup import get_logger
from utils.path_utils import is_within_configured_volumes, short_temp_name

from config import settings

logger = get_logger("partial_writes")

TEMP_PREFIX = ".partial-"
TEMP_SUFFIX = ".tmp"
LOCK_SUFFIX = ".lock"
# A temp file this young is left alone even when its lock is free.
MIN_AGE_SECONDS = 60.0

# ``.partial-`` + mkstemp's eight characters + ``.tmp``, optionally after the
# ``.<target name>`` the name carried before #331.
_LEFTOVER = re.compile(r"\.(?:.+\.)?partial-[a-z0-9_]{8}\.tmp")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
# Read and write, never truncate: an exclusive flock needs a write open on NFS.
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC
_CLAIM_ATTEMPTS = 100


def is_leftover_name(name: str) -> bool:
    """Whether a file name is one ``rewrite_zip`` gives its temp file."""
    return _LEFTOVER.fullmatch(name) is not None


def is_sidecar_name(name: str) -> bool:
    """Whether a file name is a temp file's lock file (``<temp>.lock``)."""
    return name.endswith(LOCK_SUFFIX) and is_leftover_name(name[: -len(LOCK_SUFFIX)])


# --- the journal -----------------------------------------------------------


def _journal_dir() -> Path:
    return Path(settings.data_dir) / "partial-writes"


def _marker_for(path: str) -> Path:
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:40]
    return _journal_dir() / f"{digest}.path"


def record(path: str) -> str | None:
    """Note a temp file in flight; the marker's path, or None if it can't be."""
    marker = _marker_for(path)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(os.fsencode(path))
    except OSError as exc:
        logger.debug("Could not record temp file %s: %s", path, exc)
        return None
    return str(marker)


def forget(marker: str | None) -> None:
    """Drop a marker once its temp file is placed or removed."""
    if marker:
        _unlink_quietly(marker)


def recorded_paths() -> list[str]:
    """The temp files the journal says are in flight or were left behind."""
    try:
        markers = sorted(_journal_dir().glob("*.path"))
    except OSError:
        return []
    paths = []
    for marker in markers:
        try:
            paths.append(os.fsdecode(marker.read_bytes()))
        except OSError:
            continue
    return paths


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- the writer's side -----------------------------------------------------


@dataclass
class TempClaim:
    """A temp file a writer owns: ``fd`` (open for writing) and ``path``, its
    held sidecar lock, and its journal marker. :meth:`release` once the temp
    file is placed or removed; the caller owns ``fd``."""

    fd: int
    path: str
    lock_fd: int
    lock_path: str
    marker: str | None

    def release(self) -> None:
        # The sidecar goes while its lock is still held, so no sweep can take
        # a lock on a sidecar that is about to vanish; then the marker.
        _unlink_quietly(self.lock_path)
        os.close(self.lock_fd)
        forget(self.marker)


def claim_temp(directory: str) -> TempClaim:
    """Create and lock ``<temp>.lock``, then create the temp file, then
    journal it, in that order: there is never a moment when the temp file
    exists without a held lock. All or nothing: on any failure (``flock``
    raising ``ENOLCK`` included) every descriptor is closed and every file
    this created is removed before the error propagates."""
    for _ in range(_CLAIM_ATTEMPTS):
        path = os.path.join(directory, short_temp_name(TEMP_PREFIX, TEMP_SUFFIX))
        lock_path = path + LOCK_SUFFIX
        try:
            lock_fd = os.open(lock_path, _LOCK_FLAGS | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        fd = -1
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | _CLOEXEC, 0o600)
            except FileExistsError:
                _unlink_quietly(lock_path)
                os.close(lock_fd)
                continue
            return TempClaim(fd, path, lock_fd, lock_path, record(path))
        except BaseException:
            if fd >= 0:
                os.close(fd)
                _unlink_quietly(path)
            _unlink_quietly(lock_path)
            os.close(lock_fd)
            raise
    raise FileExistsError(f"No free temp file name in {directory}")


# --- the sweep -------------------------------------------------------------


def _same_file(path: str, info: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def _open_sidecar(lock_path: str) -> int | None:
    """The sidecar, opened for a lock (created if a pre-#331 leftover has none)."""
    try:
        fd = os.open(lock_path, _LOCK_FLAGS, 0o600)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _sweep_one(path: str, min_age: float) -> bool | None:
    """Remove one leftover and its sidecar. True removed, False kept on purpose
    (its writer holds the lock, or it is too young), None not ours or couldn't
    tell."""
    if not is_leftover_name(os.path.basename(path)):
        return None
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None  # gone, a symlink (ELOOP), or unreadable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if time.time() - info.st_mtime < min_age:
            return False
        lock_path = path + LOCK_SUFFIX
        lock_fd = _open_sidecar(lock_path)
        if lock_fd is None:
            return None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return False
        except OSError:
            os.close(lock_fd)
            return None
        try:
            if not _same_file(path, info):
                return None  # the name now points at something else
            os.unlink(path)
            logger.info("Removed a temp file left by an interrupted repack: %s", path)
            _unlink_quietly(lock_path)
            return True
        finally:
            os.close(lock_fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _sweep_orphan_sidecar(lock_path: str, min_age: float) -> bool:
    """Remove a lock file whose temp file is gone, once nobody holds it."""
    temp = lock_path[: -len(LOCK_SUFFIX)]
    if os.path.lexists(temp):
        return False
    try:
        info = os.lstat(lock_path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < min_age:
        return False
    fd = _open_sidecar(lock_path)
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(temp) or not _same_file(lock_path, os.fstat(fd)):
            return False
        os.unlink(lock_path)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def sweep_folder(directory: str, *, min_age: float = MIN_AGE_SECONDS) -> int:
    """Remove the unlocked leftovers in one folder (not below it). Never raises."""
    try:
        if not is_within_configured_volumes(directory, treat_archives=False):
            return 0
        with os.scandir(directory) as entries:
            names = [
                entry.name for entry in entries
                if (is_leftover_name(entry.name) or is_sidecar_name(entry.name))
                and entry.is_file(follow_symlinks=False)
            ]
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(directory, name)
        if is_leftover_name(name):
            removed += _sweep_one(path, min_age) is True
        else:
            _sweep_orphan_sidecar(path, min_age)
    return removed


def sweep_recorded(*, min_age: float = MIN_AGE_SECONDS) -> int:
    """Startup: remove every journaled leftover nothing holds. Never raises.

    A marker is dropped once its file is gone (swept now or placed long ago).
    One whose file is held, too young, outside the Volumes or uncertain stays
    for the next start.
    """
    removed = 0
    try:
        markers = sorted(_journal_dir().glob("*.path"))
    except OSError:
        return 0
    for marker in markers:
        try:
            path = os.fsdecode(marker.read_bytes())
        except OSError:
            continue
        inside = is_within_configured_volumes(os.path.dirname(path), treat_archives=False)
        try:
            os.lstat(path)
        except FileNotFoundError:
            # Placed or removed long ago: the record is all that is left.
            if inside and is_leftover_name(os.path.basename(path)):
                _sweep_orphan_sidecar(path + LOCK_SUFFIX, min_age)
            forget(str(marker))
            continue
        except OSError:
            continue
        if inside and _sweep_one(path, min_age) is True:
            removed += 1
            forget(str(marker))
    return removed


__all__ = [
    "LOCK_SUFFIX",
    "MIN_AGE_SECONDS",
    "TEMP_PREFIX",
    "TEMP_SUFFIX",
    "TempClaim",
    "claim_temp",
    "forget",
    "is_leftover_name",
    "is_sidecar_name",
    "record",
    "recorded_paths",
    "sweep_folder",
    "sweep_recorded",
]
