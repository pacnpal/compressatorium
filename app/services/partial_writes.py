"""The hidden temp files ``archive_edit.rewrite_zip`` writes, and sweeping
the ones a hard kill left behind (#331).

A rewrite writes to ``.partial-XXXXXXXX.tmp`` beside its target and places it
only once it is checked (``.<target name>.partial-XXXXXXXX.tmp`` before #331).
A process killed hard (SIGKILL, power loss) leaves that file: hidden, ending in
``.tmp`` so library scanners skip it, but taking space forever.

* **One name pattern.** :func:`is_leftover_name` matches exactly what
  ``tempfile.mkstemp`` makes from that prefix and suffix, eight characters
  from its alphabet, in either spelling. Nothing else is ever swept.
* **A live write holds a lock.** ``rewrite_zip`` takes an exclusive ``flock``
  on its temp file the moment it creates it and keeps it until the file is
  placed or removed, in this process or any other (the CLI). The sweep only
  removes a file whose lock it can take, and that is older than
  ``MIN_AGE_SECONDS``, which covers the instant between creating the file and
  locking it.
* **Fail closed.** The folder must be inside a configured Volume
  (``is_within_configured_volumes``, which answers "outside" when it can't
  tell), the file is opened with ``O_NOFOLLOW`` and must be a regular file, and
  it is removed only while the path still names the inode that was locked.
  Anything uncertain is kept.
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
from pathlib import Path

from logging_setup import get_logger
from utils.path_utils import is_within_configured_volumes

from config import settings

logger = get_logger("partial_writes")

TEMP_PREFIX = ".partial-"
TEMP_SUFFIX = ".tmp"
# A temp file this young may belong to a write that has not taken its lock yet.
MIN_AGE_SECONDS = 60.0

# ``.partial-`` + mkstemp's eight characters + ``.tmp``, optionally after the
# ``.<target name>`` the name carried before #331.
_LEFTOVER = re.compile(r"\.(?:.+\.)?partial-[a-z0-9_]{8}\.tmp")
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def is_leftover_name(name: str) -> bool:
    """Whether a file name is one ``rewrite_zip`` gives its temp file."""
    return _LEFTOVER.fullmatch(name) is not None


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
        try:
            os.unlink(marker)
        except OSError:
            pass


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


def _sweep_one(path: str, min_age: float) -> bool | None:
    """Remove one leftover. True removed, False kept on purpose (a live write
    holds it, or it is too young), None not ours or couldn't tell."""
    if not is_leftover_name(os.path.basename(path)):
        return None
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None  # gone, a symlink (ELOOP), or unreadable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if time.time() - info.st_mtime < min_age:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError:
            return None
        try:
            current = os.lstat(path)
        except OSError:
            return None
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            return None  # the name now points at something else
        os.unlink(path)
        logger.info("Removed a temp file left by an interrupted repack: %s", path)
        return True
    except OSError:
        return None
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
                if is_leftover_name(entry.name) and entry.is_file(follow_symlinks=False)
            ]
    except OSError:
        return 0
    return sum(_sweep_one(os.path.join(directory, name), min_age) is True for name in names)


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
        try:
            os.lstat(path)
        except FileNotFoundError:
            forget(str(marker))
            continue
        except OSError:
            continue
        if not is_within_configured_volumes(os.path.dirname(path), treat_archives=False):
            continue
        if _sweep_one(path, min_age) is True:
            removed += 1
            forget(str(marker))
    return removed


__all__ = [
    "MIN_AGE_SECONDS",
    "TEMP_PREFIX",
    "TEMP_SUFFIX",
    "forget",
    "is_leftover_name",
    "record",
    "recorded_paths",
    "sweep_folder",
    "sweep_recorded",
]
