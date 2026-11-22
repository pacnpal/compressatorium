"""The hidden temp files ``archive_edit.rewrite_zip`` writes, the staging
slots a conversion writes into (``services.staging``, #398), and sweeping the
ones a hard kill left behind (#331).

A rewrite writes to ``.partial-XXXXXXXX.tmp`` beside its target and places it
only once it is checked (``.<target name>.partial-XXXXXXXX.tmp`` before #331).
A process killed hard (SIGKILL, power loss) leaves that file: hidden, ending in
``.tmp`` so library scanners skip it, but taking space forever.

* **One name pattern.** :func:`is_leftover_name` matches exactly what the
  writer names its temp file, eight characters from ``mkstemp``'s alphabet,
  in either spelling. Its lock file is :func:`sidecar_name`, a fixed-length
  name derived from the temp file's (``.partial-<16 hex>.lock``), so even a
  254-byte legacy name has one. Nothing else is ever swept.
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
  (``volume_containment(...) is True``; "can't tell" counts as outside), and
  the sweep then works through one descriptor of that folder, opened along
  its real path with no symlink followed, so a folder link repointed after
  the check cannot move it. Anything uncertain is kept.
* **Two times to sweep.** :func:`sweep_folder` runs before every rewrite into
  that folder. :func:`sweep_recorded` runs at startup, from a small journal in
  the data directory (one file per temp file in flight, written when the temp
  file is created and removed when it is gone), so a crash is cleaned up
  without walking every Volume. A record is dropped only on proof: the sweep
  removed the file, or its folder is inside a Volume, is the very folder it
  was written in (same device and inode, so not an empty mount point) and no
  longer holds it. A Volume that is away keeps the record for the next start.
* **Staging slots, by the same rules (#398).** A conversion that must stay
  hidden from a Library scanner writes into ``slot-XXXXXXXX/`` inside the
  hidden ``.compressatorium-stage/`` folder beside its output
  (``services.staging``). Its lock file is ``slot-XXXXXXXX.lock`` beside the
  slot, held from before the slot exists until it is gone, and the slot is
  journaled like a temp file. The sweep removes a slot only when it can take
  that lock, the slot is older than ``MIN_AGE_SECONDS``, and the name still
  names the same real folder (never a link), then its lock file, then the
  stage folder once it is empty. Opening a slot sweeps the stage folder first,
  so a slot whose record was lost goes too. A slot carrying
  :data:`KEEP_MARKER` (a checked output that could not be placed nor set
  aside, #391) is never swept, nor one that can't be checked for it.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from logging_setup import get_logger
from utils.junk import STAGE_DIR_NAME
from utils.path_utils import (
    companion_name,
    open_dir_beneath,
    short_temp_name,
    volume_containment,
)

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
_SIDECAR = re.compile(r"\.partial-[0-9a-f]{16}\.lock")
# A staging slot: ``slot-`` + mkstemp's eight characters; its lock file adds
# ``.lock``.
SLOT_PREFIX = "slot-"
_SLOT = re.compile(r"slot-[a-z0-9_]{8}")
# A file in a slot that no sweep removes it while it is there: the slot holds
# a checked output that could not be placed, nor set aside as a ``park-``
# slot (``staging.StagedOutput.set_aside``, #391).
KEEP_MARKER = ".compressatorium-keep"
_SLOT_LOCK = re.compile(r"slot-[a-z0-9_]{8}\.lock")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC
# Read and write, never truncate: an exclusive flock needs a write open on NFS.
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC
_CLAIM_ATTEMPTS = 100


def is_leftover_name(name: str) -> bool:
    """Whether a file name is one ``rewrite_zip`` gives its temp file."""
    return _LEFTOVER.fullmatch(name) is not None


def sidecar_name(temp_name: str) -> str:
    """The lock file's name for a temp file's name: the one rule the writer
    and the sweep share. Fixed-length, whatever the temp file is called."""
    return companion_name(temp_name, prefix=TEMP_PREFIX, suffix=LOCK_SUFFIX)


def sidecar_path(temp: str) -> str:
    """The lock file beside the temp file *temp*."""
    return os.path.join(os.path.dirname(temp), sidecar_name(os.path.basename(temp)))


def is_sidecar_name(name: str) -> bool:
    """Whether a file name is a temp file's lock file."""
    return _SIDECAR.fullmatch(name) is not None


def is_slot_name(name: str) -> bool:
    """Whether a name is a staging slot's (``services.staging``)."""
    return _SLOT.fullmatch(name) is not None


def slot_lock_name(slot_name: str) -> str:
    """A staging slot's lock file, beside it in the stage folder: the one
    rule the writer and the sweep share."""
    return slot_name + LOCK_SUFFIX


def _is_slot_lock_name(name: str) -> bool:
    return _SLOT_LOCK.fullmatch(name) is not None


# --- the journal -----------------------------------------------------------


def _journal_dir() -> Path:
    return Path(settings.data_dir) / "partial-writes"


def _marker_for(path: str) -> Path:
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:40]
    return _journal_dir() / f"{digest}.path"


def _folder_id(folder: str) -> tuple[int, int]:
    info = os.stat(folder)
    return info.st_dev, info.st_ino


def record(path: str) -> str | None:
    """Note a temp file in flight, with the identity of the folder it is in;
    the marker's path, or None if it can't be."""
    marker = _marker_for(path)
    try:
        dev, ino = _folder_id(os.path.dirname(path))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(os.fsencode(path) + b"\0" + f"{dev}:{ino}".encode())
    except OSError as exc:
        logger.debug("Could not record temp file %s: %s", path, exc)
        return None
    return str(marker)


def forget(marker: str | None) -> None:
    """Drop a marker once its temp file is placed or removed."""
    if marker:
        _unlink_quietly(marker)


def _read_marker(marker: Path) -> tuple[str, tuple[int, int] | None]:
    """A marker's temp file path and its folder's (dev, ino), if it has one."""
    raw, _, folder = marker.read_bytes().partition(b"\0")
    try:
        dev, ino = (int(part) for part in folder.split(b":"))
    except ValueError:
        return os.fsdecode(raw), None
    return os.fsdecode(raw), (dev, ino)


def recorded_paths() -> list[str]:
    """The temp files the journal says are in flight or were left behind."""
    try:
        markers = sorted(_journal_dir().glob("*.path"))
    except OSError:
        return []
    paths = []
    for marker in markers:
        try:
            paths.append(_read_marker(marker)[0])
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
    file is placed or removed, or a removal failed; the caller owns ``fd``."""

    fd: int
    path: str
    lock_fd: int
    lock_path: str
    marker: str | None

    def release(self, *, temp_gone: bool = True) -> None:
        """Drop the lock, and the journal marker only when ``temp_gone``: a
        temp file whose removal failed stays journaled for the startup sweep.
        """
        # The sidecar goes while its lock is still held, so no sweep can take
        # a lock on a sidecar that is about to vanish; then the marker.
        _unlink_quietly(self.lock_path)
        os.close(self.lock_fd)
        if temp_gone:
            forget(self.marker)


def claim_temp(directory: str) -> TempClaim:
    """Create and lock ``<temp>.lock``, then create the temp file, then
    journal it, in that order: there is never a moment when the temp file
    exists without a held lock. All or nothing: on any failure (``flock``
    raising ``ENOLCK`` included) every descriptor is closed and every file
    this created is removed before the error propagates."""
    for _ in range(_CLAIM_ATTEMPTS):
        path = os.path.join(directory, short_temp_name(TEMP_PREFIX, TEMP_SUFFIX))
        lock_path = sidecar_path(path)
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
#
# Every operation on a candidate runs relative to one directory descriptor,
# opened right after the folder passed containment, by a walk that refuses
# every symlink (``open_dir_beneath``). Repointing a folder symlink after the
# check therefore cannot move the sweep anywhere else.


def _open_folder(directory: str) -> int | None:
    """The folder, opened once and bound: its real path must be inside a
    configured Volume (``volume_containment(...) is True``), and it is opened
    along that real path with no symlink followed. None when it is outside,
    containment can't tell, or the walk fails (an unmounted Volume, a
    component swapped for a link)."""
    try:
        real = os.path.realpath(directory)
        if volume_containment(real, treat_archives=False) is not True:
            return None
        return open_dir_beneath(real)
    except (OSError, ValueError):
        return None


def same_file(dir_fd: int, name: str, info: os.stat_result) -> bool:
    """Whether *name* in *dir_fd* is still the file *info* describes (same
    device and inode, no link followed). Shared with ``staging``: a lock
    taken by flock counts only while its file is still the one the name
    points at, since a holder may unlink it between an open and the
    flock."""
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def _open_sidecar(dir_fd: int, lock_name: str) -> int | None:
    """The sidecar, opened for a lock (created if a pre-#331 leftover has none)."""
    try:
        fd = os.open(lock_name, _LOCK_FLAGS, 0o600, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _unlink_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _sweep_one(dir_fd: int, name: str, min_age: float, *, where: str) -> bool | None:
    """Remove one leftover and its sidecar from the folder ``dir_fd``. True
    removed, False kept on purpose (its writer holds the lock, or it is too
    young), None not ours or couldn't tell. ``where`` names the folder in
    the log."""
    if not is_leftover_name(name):
        return None
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None  # gone, a symlink (ELOOP), or unreadable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if time.time() - info.st_mtime < min_age:
            return False
        lock_name = sidecar_name(name)
        lock_fd = _open_sidecar(dir_fd, lock_name)
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
            if not same_file(dir_fd, name, info) or not same_file(
                dir_fd, lock_name, os.fstat(lock_fd)
            ):
                return None  # a name now points at something else
            os.unlink(name, dir_fd=dir_fd)
            logger.info(
                "Removed a temp file left by an interrupted repack: %s",
                os.path.join(where, name),
            )
            _unlink_at(dir_fd, lock_name)
            return True
        finally:
            os.close(lock_fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _sweep_orphan_sidecar(
    dir_fd: int, lock_name: str, min_age: float, temp_name: str | None = None
) -> bool:
    """Remove a lock file whose temp file is gone, once nobody holds it.
    ``temp_name``, when known, must not exist; the folder sweep passes None
    for a lock file no leftover in the folder maps to."""

    def temp_exists() -> bool:
        if temp_name is None:
            return False
        try:
            os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True  # can't tell: keep the lock file
        return True

    if temp_exists():
        return False
    try:
        info = os.stat(lock_name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < min_age:
        return False
    fd = _open_sidecar(dir_fd, lock_name)
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if temp_exists() or not same_file(dir_fd, lock_name, os.fstat(fd)):
            return False
        os.unlink(lock_name, dir_fd=dir_fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def sweep_folder(directory: str, *, min_age: float = MIN_AGE_SECONDS) -> int:
    """Remove the unlocked leftovers in one folder (not below it). Never raises."""
    dir_fd = _open_folder(directory)
    if dir_fd is None:
        return 0
    try:
        try:
            with os.scandir(dir_fd) as entries:
                names = [
                    entry.name for entry in entries
                    if (is_leftover_name(entry.name) or is_sidecar_name(entry.name))
                    and entry.is_file(follow_symlinks=False)
                ]
        except OSError:
            return 0
        leftovers = [name for name in names if is_leftover_name(name)]
        in_use = {sidecar_name(name) for name in leftovers}
        removed = sum(
            _sweep_one(dir_fd, name, min_age, where=directory) is True for name in leftovers
        )
        for name in names:
            if is_sidecar_name(name) and name not in in_use:
                _sweep_orphan_sidecar(dir_fd, name, min_age)
        return removed
    finally:
        os.close(dir_fd)


def _sweep_marker(marker: Path, min_age: float) -> str | None:
    """Sweep one journaled temp file: "removed" (this sweep removed it),
    "gone" (proven gone already), or None (keep the marker)."""
    try:
        path, folder_id = _read_marker(marker)
    except OSError:
        return None
    folder, name = os.path.split(path)
    if is_slot_name(name):
        return _sweep_slot_marker(folder, name, folder_id, min_age)
    dir_fd = _open_folder(folder)
    if dir_fd is None:
        return None  # outside, can't tell, or the Volume is away: keep it
    try:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            # Missing proves nothing unless this is the very folder the temp
            # file was made in: an empty mount point has another identity.
            try:
                info = os.fstat(dir_fd)
            except OSError:
                return None
            if folder_id != (info.st_dev, info.st_ino):
                return None
            if is_leftover_name(name):
                _sweep_orphan_sidecar(dir_fd, sidecar_name(name), min_age, temp_name=name)
            return "gone"
        except OSError:
            return None
        return "removed" if _sweep_one(dir_fd, name, min_age, where=folder) is True else None
    finally:
        os.close(dir_fd)


# --- staging slots (#398) ----------------------------------------------------


def open_stage_folder(folder_fd: int, *, create: bool) -> int:
    """The stage folder inside the output folder ``folder_fd``, opened without
    following a link (a link in its place fails, usually ``ELOOP``); made
    first, mode 0700, when *create*. Raises ``OSError``."""
    if create:
        try:
            os.mkdir(STAGE_DIR_NAME, 0o700, dir_fd=folder_fd)
        except FileExistsError:
            pass
    return os.open(
        STAGE_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC,
        dir_fd=folder_fd,
    )


def remove_stage_folder_if_empty(folder_fd: int) -> None:
    """Remove the stage folder once no slot is left in it. Never raises."""
    try:
        os.rmdir(STAGE_DIR_NAME, dir_fd=folder_fd)
    except OSError:
        pass  # not empty (another job's slot), already gone, or can't


def _marked_kept(stage_fd: int, name: str) -> bool:
    """Whether the slot *name* carries :data:`KEEP_MARKER`, or can't be
    checked for it (fail closed: kept)."""
    try:
        os.stat(f"{name}/{KEEP_MARKER}", dir_fd=stage_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _sweep_slot(stage_fd: int, name: str, min_age: float, *, where: str) -> bool | None:
    """Remove one staging slot and its lock file from the stage folder
    ``stage_fd``. True removed, False kept on purpose (its writer holds the
    lock, or it is too young), None not ours or couldn't tell."""
    if not is_slot_name(name):
        return None
    try:
        info = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None  # a link or a file named like a slot: never ours
    if time.time() - info.st_mtime < min_age:
        return False
    if _marked_kept(stage_fd, name):
        return False  # holds a checked output that is the only copy (#391)
    lock_name = slot_lock_name(name)
    lock_fd = _open_sidecar(stage_fd, lock_name)
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
        if not same_file(stage_fd, name, info) or not same_file(
            stage_fd, lock_name, os.fstat(lock_fd)
        ):
            return None  # a name now points at something else
        # Once more under the lock: a writer may have
        # marked the slot kept and let go of its lock since the first look.
        # The same check, so one that can't be made keeps it.
        if _marked_kept(stage_fd, name):
            return False
        # Descriptor-based (``rmtree`` with ``dir_fd``): never follows a link
        # inside the slot.
        shutil.rmtree(name, dir_fd=stage_fd)
        logger.info(
            "Removed the staged output of an interrupted conversion: %s",
            os.path.join(where, name),
        )
        _unlink_at(stage_fd, lock_name)
        return True
    except OSError:
        return None
    finally:
        os.close(lock_fd)


def sweep_stage(stage_fd: int, *, min_age: float = MIN_AGE_SECONDS, where: str = "") -> int:
    """Remove the unlocked, old slots in an open stage folder, and the lock
    files whose slot is gone. Never raises."""
    try:
        with os.scandir(stage_fd) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        return 0
    removed = sum(
        _sweep_slot(stage_fd, name, min_age, where=where) is True
        for name in names if is_slot_name(name)
    )
    for name in names:
        if _is_slot_lock_name(name):
            _sweep_orphan_sidecar(
                stage_fd, name, min_age, temp_name=name[: -len(LOCK_SUFFIX)],
            )
    return removed


def _sweep_slot_marker(
    stage: str, name: str, folder_id: tuple[int, int] | None, min_age: float,
) -> str | None:
    """Sweep one journaled staging slot: "removed", "gone" (proven gone
    already), or None (keep the marker). Then removes the stage folder if
    nothing is left in it."""
    folder_fd = _open_folder(os.path.dirname(stage))
    if folder_fd is None:
        return None  # outside, can't tell, or the Volume is away: keep it
    try:
        try:
            stage_fd = open_stage_folder(folder_fd, create=False)
        except FileNotFoundError:
            # No stage folder at all: gone, but only on the very filesystem
            # it was made on (an empty mount point is another device).
            try:
                device = os.fstat(folder_fd).st_dev
            except OSError:
                return None
            return "gone" if folder_id is not None and device == folder_id[0] else None
        except OSError:
            return None
        try:
            outcome = _sweep_slot_in(stage_fd, stage, name, folder_id, min_age)
        finally:
            os.close(stage_fd)
        if outcome is not None:
            remove_stage_folder_if_empty(folder_fd)
        return outcome
    finally:
        os.close(folder_fd)


def _sweep_slot_in(
    stage_fd: int, stage: str, name: str, folder_id: tuple[int, int] | None,
    min_age: float,
) -> str | None:
    """:func:`_sweep_slot_marker` once the stage folder is open."""
    try:
        info = os.fstat(stage_fd)
    except OSError:
        return None
    if folder_id != (info.st_dev, info.st_ino):
        return None  # not the stage folder the slot was made in
    try:
        os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
    except FileNotFoundError:
        _sweep_orphan_sidecar(stage_fd, slot_lock_name(name), min_age, temp_name=name)
        return "gone"
    except OSError:
        return None
    return "removed" if _sweep_slot(stage_fd, name, min_age, where=stage) is True else None


def sweep_recorded(*, min_age: float = MIN_AGE_SECONDS) -> int:
    """Startup: remove every journaled leftover nothing holds, temp files and
    staging slots alike (#398), by the same rules. Never raises.

    A marker is dropped only on proof the file is gone: this sweep removed
    it, or its folder is inside a Volume, is the folder it was written in
    (same device and inode) and no longer holds it. A file that is held or
    too young, a Volume that is away (unmounted, or an empty mount point in
    its place), and anything uncertain keep the marker for the next start.
    """
    removed = 0
    try:
        markers = sorted(_journal_dir().glob("*.path"))
    except OSError:
        return 0
    for marker in markers:
        outcome = _sweep_marker(marker, min_age)
        if outcome is not None:
            removed += outcome == "removed"
            forget(str(marker))
    return removed


__all__ = [
    "KEEP_MARKER",
    "LOCK_SUFFIX",
    "MIN_AGE_SECONDS",
    "SLOT_PREFIX",
    "TEMP_PREFIX",
    "TEMP_SUFFIX",
    "TempClaim",
    "claim_temp",
    "forget",
    "is_leftover_name",
    "is_sidecar_name",
    "is_slot_name",
    "open_stage_folder",
    "record",
    "recorded_paths",
    "remove_stage_folder_if_empty",
    "sidecar_name",
    "sidecar_path",
    "slot_lock_name",
    "sweep_folder",
    "sweep_recorded",
    "sweep_stage",
]
