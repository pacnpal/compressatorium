"""What the file Referrers (``.m3u``, ``gamelist.xml``) share (#386).

A file Referrer is a text file inside a Volume that names Images by path. Each
one reads its own format, but finding the files, matching an entry to a
replaced file, and writing the file back safely are the same for every one:

- :class:`Renames` holds each replaced file's identity and where it goes, so an
  entry is matched by ``(st_dev, st_ino)`` (``same_identity``), whatever
  spelling reaches the file, and by name for the fail-safe check.
- :func:`search_dirs` is the Image's folder and each folder above it up to its
  Volume root: where a playlist or a Platform folder's gamelist sits.
- :func:`points_at` says whether a written entry reaches the output.
- :func:`may_read` decides, at planning, whether a file is read at all.
- :func:`rewrite_in_place` is the one apply: it checks the file again, takes
  the in-process and cross-process locks, re-reads it, rewrites what is there
  now, keeps a one-time ``.bak`` and replaces it atomically, guarded by
  ``check_write``.
"""

from __future__ import annotations

import functools
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from services.lock_manager import LockWaitTimeout, lock_manager
from utils.path_utils import (
    FileIdentity,
    file_identity,
    lock_boundary_root,
    same_identity,
    volume_containment,
)

from .base import (
    ReferrerError,
    atomic_write_bytes,
    check_write,
    record_write,
    staged_output,
)

BACKUP_SUFFIX = ".bak"

# One lock per resolved file path, held across read-rewrite-write.
# ponytail: entries are never removed; one small lock per file ever touched.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def target_lock(path: str) -> threading.Lock:
    """The in-process lock for the Referrer file at *path*."""
    key = os.path.realpath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


@dataclass(frozen=True)
class Target:
    old_dir: str  # resolved folder of the replaced file
    new: str      # resolved output path


@dataclass(frozen=True)
class Renames:
    # Each replaced file's identity and where it goes. Matched with
    # `same_identity`, which tolerates the unstable inodes of FUSE/SMB mounts.
    replaced: tuple[tuple[FileIdentity, Target], ...]
    names: frozenset[str]  # folded file names of the replaced files

    def target_for(self, identity: FileIdentity | None) -> Target | None:
        """The rename for the file *identity* is, if it is a replaced file."""
        return next((t for old, t in self.replaced if same_identity(identity, old)), None)

    @classmethod
    def of(
        cls, renames: Mapping[str, str],
        name_forms: Callable[[str], Iterable[str]] = lambda name: (name,),
    ) -> Renames:
        """*renames* (``{old_path: new_path}``) as identities and folded names.

        *name_forms* gives the ways a reader could write each replaced file's
        name (the ``.m3u`` reader trims ASCII spaces, say); empty ones are
        dropped. A replaced file that can't be stat'ed is matched by name only.
        """
        replaced = []
        for old, new in renames.items():
            identity = file_identity(old)
            if identity is not None:
                replaced.append((identity, Target(
                    os.path.realpath(os.path.dirname(old)), os.path.realpath(new),
                )))
        names = frozenset(
            fold(form)
            for old in renames
            for form in name_forms(os.path.basename(old))
            if form
        )
        return cls(tuple(replaced), names)


def fold(name: str) -> str:
    """Compare file names as a person would: same Unicode form, any case."""
    return unicodedata.normalize("NFC", name).casefold()


def named_file(path: str) -> str:
    """The file name an entry path names, split on either separator, folded."""
    return fold(re.split(r"[\\/]", path)[-1])


def search_dirs(path: str) -> list[str]:
    """*path*'s resolved folder and each folder above it up to its Volume root.

    Raises ReferrerError when the Volume boundary can't be determined (the
    fresh root probe failed or went over its bound): the folders above could
    hold a file naming the Source, so it must be kept.
    """
    start = os.path.dirname(os.path.realpath(path))
    boundary = lock_boundary_root(start)
    if boundary is None:
        raise ReferrerError(
            start, "can't confirm its Volume boundary, so the files above it weren't checked",
        )
    root = str(boundary)
    dirs = [start]
    if not (start == root or start.startswith(root.rstrip(os.sep) + os.sep)):
        return dirs
    current = start
    while current != root:
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        dirs.append(current)
    return dirs


def dirs_for(renames: Mapping[str, str]) -> list[str]:
    """Every folder :func:`search_dirs` gives for any replaced file, in order, once."""
    dirs: list[str] = []
    for old in renames:
        for directory in search_dirs(old):
            if directory not in dirs:
                dirs.append(directory)
    return dirs


def points_at(base: str, entry: str, target: Target, *, strict: bool) -> bool:
    """Whether *entry* (a posix path), read from *base*, reaches the output.

    *strict* (applying a change) requires the output to exist, so a late,
    abandoned apply can't point a file at an output removed meanwhile.
    Otherwise (the delete confirmation, planning before the output exists) the
    resolved paths are compared.

    While the output is still staged for a Referrer that places it (the RomM
    swap, #391), its final path is empty: the entry must resolve to that path,
    and, when *strict*, the staged output must still be there (the hook's
    guard checks it is the same file before each write).
    """
    path = os.path.join(base, entry)
    staged = staged_output(target.new)
    if staged is not None:
        return os.path.realpath(path) == target.new and (
            not strict or file_identity(staged) is not None
        )
    output = file_identity(target.new)
    if output is not None:
        return same_identity(file_identity(path), output)
    if strict:
        return False
    return os.path.realpath(path) == target.new


def read_target(path: str) -> bytes:
    """The bytes of the Referrer file at *path*, or ReferrerError naming it."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ReferrerError(path, f"can't read: {exc.strerror}") from exc


def may_read(path: str) -> bool:
    """Whether planning reads the Referrer file at *path*.

    Never through a link (it may lead out of the Volumes): that keeps the
    Source. Which files are read and written is decided against the Volumes as
    they are now: one confirmed outside them is left alone (False); one we
    can't tell about (a root probe failed) keeps the Source.
    """
    if os.path.islink(path):
        raise ReferrerError(path, "is a symlink")
    inside = volume_containment(path, treat_archives=False)
    if inside is None:
        raise ReferrerError(path, "can't confirm it is inside the configured Volumes")
    return inside


def rewrite_in_place(
    path: str, rewrite: Callable[[bytes], bytes | None], *,
    read: Callable[[str], bytes] = read_target, wait: float,
) -> None:
    """Apply one planned change to the Referrer file at *path*.

    Checked again right before writing, against the Volumes as they are now:
    a file that has left them since planning is not written (it was planned
    because it named the Source, so the Source is kept); outside and
    "couldn't tell" both refuse, as do a symlink and a read-only file.

    Threads take the in-process lock; other processes (a CLI run beside the
    server, #306) the cross-process Referrer lock, waiting up to *wait*
    seconds (and no longer than the hook waits, ``check_write``). Both are
    held over the whole read, rewrite and replace so no update is lost.

    *rewrite* gets the bytes there now and returns the new bytes, or None
    when nothing needs changing; it raises ReferrerError when it can't. The
    first change keeps the original as ``<file>.bak`` (never overwritten).
    """
    if os.path.islink(path):
        raise ReferrerError(path, "is a symlink")
    if volume_containment(path, treat_archives=False) is not True:
        raise ReferrerError(path, "isn't confirmed inside the configured Volumes")
    if not os.access(path, os.W_OK):
        raise ReferrerError(path, "is read-only")
    with target_lock(path):
        try:
            with lock_manager.referrer_lock(
                path, wait=wait, keep_waiting=lambda: check_write(path),
            ):
                _rewrite_locked(path, rewrite, read)
        except LockWaitTimeout as exc:
            raise ReferrerError(path, "is being updated by another job") from exc


def _rewrite_locked(
    path: str, rewrite: Callable[[bytes], bytes | None], read: Callable[[str], bytes],
) -> None:
    """Read, rewrite and replace, under both locks."""
    original = read(path)
    updated = rewrite(original)
    if updated is None:
        return
    try:
        backup = path + BACKUP_SUFFIX
        # The guard runs right before each file is published, after its temp
        # copy is synced: a write that stalled while the job gave up (or the
        # output changed) never lands.
        publish_ok = functools.partial(check_write, path)
        if not os.path.lexists(backup):
            atomic_write_bytes(backup, original, like=path, guard=publish_ok)
        atomic_write_bytes(path, updated, guard=publish_ok)
    except OSError as exc:
        raise ReferrerError(path, exc.strerror or str(exc)) from exc
    # Landed: what an Image rename puts back if it doesn't go ahead (#425).
    record_write(path, original, updated)


def restore_in_place(path: str, before: bytes, after: bytes, *, wait: float) -> None:
    """Put *before* back in the Referrer file at *path*, which the hook
    rewrote to *after* (an Image rename that didn't go ahead, #425).

    The same checks and locks as :func:`rewrite_in_place`. Only a file that
    still holds exactly *after* is written: one changed since (by a person,
    or another job) is left as it is and ReferrerError says so. Exact bytes,
    never a re-plan, so nothing the rename didn't change is touched."""
    if os.path.islink(path):
        raise ReferrerError(path, "is a symlink")
    if volume_containment(path, treat_archives=False) is not True:
        raise ReferrerError(path, "isn't confirmed inside the configured Volumes")
    with target_lock(path):
        try:
            with lock_manager.referrer_lock(path, wait=wait):
                if read_target(path) != after:
                    raise ReferrerError(path, "changed since the rename updated it")
                try:
                    atomic_write_bytes(path, before)
                except OSError as exc:
                    raise ReferrerError(path, exc.strerror or str(exc)) from exc
        except LockWaitTimeout as exc:
            raise ReferrerError(path, "is being updated by another job") from exc
