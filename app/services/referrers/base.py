"""The Referrer contract (#295).

A Referrer is anything that points at an Image by its path: an ``.m3u``
playlist, a frontend's ``gamelist.xml`` (#386), later RomM's ROM. When a conversion
replaces a Source, every Referrer that points at it is moved to the output
before the Source is removed.

Three operations. ``detect`` and ``plan`` are synchronous and run off the
event loop, on the hook's detached thread:

- ``detect(image_path)``: cheap check whether this Referrer could point at the
  Image at all (for ``.m3u``: is there a playlist beside it).
- ``plan(renames)``: the changes a set of ``{old_path: new_path}`` renames
  needs, one ``ReferrerChange`` per file or record. Planning writes nothing,
  so the delete confirmation can show the result.
- ``apply(change)``: write one planned change atomically, keeping a one-time
  backup. Raises ``ReferrerError`` when it can't. A plain ``def`` runs on the
  detached thread; an ``async def`` (a network Referrer) is awaited on the
  event loop under the hook's bound and its Cancel, so it must never block.

A Referrer that reads its Library's settings (the gamelist Referrer's ES-DE
gamelists folder, #387) overrides ``for_library(library)``, which the
registry calls with the Image's Library and which returns the Referrer to
use for it (by default the Referrer itself).

Two declarations (#383), checked when the Referrer is registered:

- ``opt_in``: False for a Referrer that is always on (``.m3u``); True for one
  that runs only where the Image's Library turns it on
  (``Library.referrer_enabled``).
- ``phase``: a :class:`Phase`. File Referrers run first; a later phase runs
  only when every Referrer of the earlier phases succeeded.
"""

from __future__ import annotations

import contextvars
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from utils.path_utils import (
    FileIdentity,
    file_identity,
    place_without_clobbering,
    same_identity,
    short_temp_file,
)


class ReferrerError(Exception):
    """A Referrer could not be planned or applied. ``target`` names what failed."""

    def __init__(self, target: str, reason: str):
        super().__init__(f"{target}: {reason}")
        self.target = target
        self.reason = reason


@dataclass(frozen=True)
class ReferrerChange:
    """One planned rewrite of one Referrer target."""

    referrer: str
    target: str
    # (old entry, new entry) pairs, as shown in the delete confirmation.
    edits: tuple[tuple[str, str], ...]
    # Whatever the Referrer needs to apply the change; never sent to clients.
    payload: Any = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "referrer": self.referrer,
            "target": self.target,
            # How many entries change, for "gamelist.xml, 3 entries" (#386).
            "entries": len(self.edits),
            "edits": [{"old": old, "new": new} for old, new in self.edits],
        }


class Phase(IntEnum):
    """When a Referrer runs. Lower runs first.

    A phase runs only when every Referrer of the phases before it succeeded,
    so a file Referrer that fails leaves another app's record untouched.
    """

    # Local files that point at the Image (.m3u, gamelist.xml).
    FILE = 10
    # Another app's record, reached over the network (RomM).
    NETWORK = 20

    @property
    def target(self) -> str:
        """What the phase updates, named when it times out or is cancelled."""
        return "playlists and gamelists" if self is Phase.FILE else "linked records"


class Referrer(ABC):
    """A registry-driven Referrer plugin. ``id`` is stable; it lands in the Ledger.

    Every Referrer declares ``opt_in`` and ``phase`` (see the module docstring);
    the registry refuses one that doesn't.
    """

    id: str
    label: str
    opt_in: bool
    phase: Phase

    @abstractmethod
    def detect(self, image_path: str) -> bool:
        """Whether this Referrer could point at the Image at *image_path*."""

    @abstractmethod
    def plan(self, renames: Mapping[str, str]) -> list[ReferrerChange]:
        """The changes *renames* (``{old_path: new_path}``) needs. Writes nothing."""

    def for_library(self, library) -> Referrer:
        """This Referrer as it runs for an Image in *library* (#387).

        The registry calls it for each active Referrer with the Image's
        Library (None when it is in none). A Referrer whose behaviour depends
        on its Library's settings (``library.referrer_options``) returns a
        copy bound to them; the registered one is never changed. Reads
        settings in memory only. Raising keeps the Source.
        """
        return self

    @abstractmethod
    def apply(self, change: ReferrerChange) -> None:
        """Write *change* atomically with a one-time backup, or raise ReferrerError.

        May be ``async def``: the hook then awaits it on the event loop,
        bounded and cancellable, instead of calling it on its thread.
        """


@dataclass
class WriteGuard:
    """What must still hold when a Referrer writes, checked under its lock.

    The hook runs on a thread it can abandon (timeout, Cancel). After that the
    job lets go of the output lock and another conversion may replace the
    output, so a late write could point a playlist at a different disc.
    ``abandoned`` is set by the hook when it stops waiting; ``outputs`` holds
    each output's identity captured when the hook started, compared with
    ``same_identity`` (which tolerates the unstable inodes of FUSE/SMB mounts).
    """

    abandoned: threading.Event
    outputs: dict[str, FileIdentity | None]

    def check(self, target: str) -> None:
        if self.abandoned.is_set():
            raise ReferrerError(target, "the job stopped waiting for it; not writing")
        for path, identity in self.outputs.items():
            if not same_identity(file_identity(path), identity):
                raise ReferrerError(
                    target, f"the output {os.path.basename(path)} changed after its check",
                )


# The guard ``check_write`` consults. A context variable, not thread-local
# state, so one mechanism covers both kinds of apply (codex 4588646144): the
# hook's worker thread sets it for its plain applies, and each async apply's
# task runs in a context that holds it (``guarded_context``).
_guard: contextvars.ContextVar[WriteGuard | None] = contextvars.ContextVar(
    "referrer_write_guard", default=None,
)


@contextmanager
def write_guard(guard: WriteGuard):
    """Make *guard* the one ``check_write`` consults in this context."""
    token = _guard.set(guard)
    try:
        yield guard
    finally:
        _guard.reset(token)


def guarded_context(guard: WriteGuard) -> contextvars.Context:
    """A copy of the current context in which ``check_write`` consults
    *guard*: an async apply's task runs in it."""
    context = contextvars.copy_context()
    context.run(_guard.set, guard)
    return context


def check_write(target: str) -> None:
    """Raise ReferrerError unless writing *target* is still allowed. Call it
    under the target's lock, right before each write. A no-op outside the hook
    (planning, or a caller that writes a new playlist).

    It stats each output, so an async apply on a slow mount calls it off the
    loop with its context (``asyncio.to_thread`` copies it)."""
    guard = _guard.get()
    if guard is not None:
        guard.check(target)


def _keep_owner(tmp: str, original: os.stat_result) -> None:
    """Give *tmp* the replaced file's group (and owner, when running as root).

    Readers that reach a shared playlist through its group would lose access
    if the replacement took the writer's group, so a group that can't be kept
    aborts the write.
    """
    uid = original.st_uid if hasattr(os, "geteuid") and os.geteuid() == 0 else -1
    current = os.stat(tmp)
    if current.st_gid == original.st_gid and uid in (-1, current.st_uid):
        return
    try:
        os.chown(tmp, uid, original.st_gid)
    except OSError as exc:
        raise PermissionError(
            exc.errno, f"can't keep its group ({exc.strerror or exc})",
        ) from exc


def _temp_file(directory: str) -> tuple[int, str]:
    """A new hidden temp file in *directory*. Its name is short and fixed-length
    (``.tmp-`` plus random characters), never built from the target's name, so a
    target near the 255-byte file name limit can still be written."""
    return short_temp_file(directory, prefix=".tmp-")


def atomic_write_bytes(
    path: str, data: bytes, *, like: str | None = None,
    guard: Callable[[], None] | None = None,
) -> None:
    """Replace *path* with *data* so a crash leaves the old file or the new one.

    Writes a temp file in the same directory, fsyncs it and renames it over
    *path*. The permission bits and group (and owner, when running as root)
    come from *like* when given, else from the file being replaced; a group
    that can't be kept aborts the write. A new file with neither gets 0644.

    *guard* runs once the temp file is fully written and synced, immediately
    before it is published. If it raises, the temp file is removed and the
    error propagates, so nothing is published (a write that stalled in fsync
    while its caller gave up must not land afterwards).
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = _temp_file(directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            original = os.stat(like or path)
        except FileNotFoundError:
            if like is not None:
                # The file whose mode and group this copy must take is gone:
                # don't fall back to 0644 for something meant to be private.
                raise
            original = None
        if original is not None:
            _keep_owner(tmp, original)
        os.chmod(tmp, original.st_mode & 0o7777 if original is not None else 0o644)
        if guard is not None:
            guard()
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def write_new_bytes(
    path: str, data: bytes, *, guard: Callable[[], None] | None = None,
) -> None:
    """Create *path* holding *data*, or raise ``FileExistsError`` if anything is there.

    The no-overwrite sibling of ``atomic_write_bytes``. It writes and fsyncs a
    temp file in the same directory, then places it with
    ``place_without_clobbering`` (a hard link or a no-replace rename), so the
    existence check and the creation are one atomic step: two writers racing
    for one name have exactly one winner, and a file or symlink already at
    *path* is never touched. The new file has the default ``0o644`` mode.

    *guard* is the same publish step as in ``atomic_write_bytes``: it runs once
    the temp file is written and synced, right before it is placed, and if it
    raises nothing is published.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = _temp_file(directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        if guard is not None:
            guard()
        place_without_clobbering(tmp, path)
    finally:
        with suppress(OSError):
            os.unlink(tmp)
