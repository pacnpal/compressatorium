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
  backup. Raises ``ReferrerError`` when it can't. Always synchronous; the
  hook runs it on the detached thread.
- ``apply_async(change)``: the async form. A network Referrer overrides it
  (#391); the hook then awaits it on the event loop under the hook's bound
  and its Cancel, so it must never block. By default it runs ``apply`` on a
  worker thread.

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

And one optional answer (#391): ``places_output`` says the Referrer moves the
job's staged output into place itself (the RomM swap renames the ROM's file
onto the output's name before the output is there). The job then keeps the
output staged and hands the hook a :class:`StagedPlacement`, which applies
read with ``staged_placement()``. An apply may return :class:`Applied` naming
Source files it removed itself.

An Image rename (#425) runs the same hook with a :class:`StagedPlacement`
whose ``rename`` is set: each new name's "staged" file is the Image's own file,
still under its old name, and placing it is the rename. File Referrers treat
it like any staged output; a Referrer that renames the file itself (RomM,
whose ROM rename moves it) checks ``rename``. Planning for the rename preview
sees the same placement (``planning``). ``rename_refused`` lets an opt-in
Referrer that is off say why an Image must not be renamed without it.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
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
    """A Referrer could not be planned or applied. ``target`` names what failed.

    ``kept`` is what the job says when this keeps the Source, in place of
    "couldn't update <target>": "RomM refused the rename" (#391).
    """

    def __init__(self, target: str, reason: str, *, kept: str | None = None):
        super().__init__(f"{target}: {reason}")
        self.target = target
        self.reason = reason
        self.kept = kept


@dataclass(frozen=True)
class ReferrerChange:
    """One planned rewrite of one Referrer target."""

    referrer: str
    target: str
    # (old entry, new entry) pairs, as shown in the delete confirmation.
    edits: tuple[tuple[str, str], ...]
    # Whatever the Referrer needs to apply the change; never sent to clients.
    payload: Any = field(default=None, compare=False, repr=False)
    # One line the delete confirmation shows for it, when the edits alone
    # don't say it: "RomM: rename ROM 12 to Game.chd" (#391).
    summary: str | None = None
    # Fields the Ledger's record of an applied change carries besides its
    # target and entry count: the RomM swap's ``rom_id`` (#391).
    record: Mapping[str, Any] | None = field(default=None, compare=False)

    def to_dict(self) -> dict:
        out = {
            "referrer": self.referrer,
            "target": self.target,
            # How many entries change, for "gamelist.xml, 3 entries" (#386).
            "entries": len(self.edits),
            "edits": [{"old": old, "new": new} for old, new in self.edits],
        }
        if self.summary:
            out["summary"] = self.summary
        return out


@dataclass(frozen=True)
class Applied:
    """What an apply did beyond its target, when it returns one (#391).

    ``removed``: Source files the apply has already removed itself (the RomM
    swap deletes the Source RomM moved, once the rehash is confirmed). The job
    records them as deleted and does not try to delete them again.
    """

    removed: tuple[str, ...] = ()
    # One line the job, and the Ledger's record of the change, says: what
    # the user should know about how it was applied (#391).
    note: str | None = None


@dataclass(frozen=True)
class StagedPlacement:
    """The job's outputs, still hidden in a staging slot while the hook runs (#391).

    A job whose output a Referrer places itself (``Referrer.places_output``)
    keeps it staged until the Referrer hook: the RomM swap must rename the
    ROM's file onto the output's name before the output is there. ``staged``
    maps each output's final path to where it is staged; ``place`` moves them
    all into place (the job's own placing, which carries their records) and
    does nothing once they are placed; ``is_placed`` says whether they are,
    from the slot itself.

    ``held`` is set by a Referrer that stopped part way and left its work for
    the next run (#391): the job then never places the output, since the
    name it would take may hold, or be owed to, the Source.
    """

    staged: Mapping[str, str]
    place: Callable[[], Awaitable[None]]
    is_placed: Callable[[], bool] = field(default=lambda: False, compare=False)
    # The same placing, blocking and with no hand-off of the job's locks, for
    # a Referrer that runs it as a step of its own it always waits for (the
    # RomM swap). None: only ``place`` is offered.
    place_blocking: Callable[[], None] | None = field(default=None, compare=False)
    held: threading.Event = field(default_factory=threading.Event, compare=False)
    # The Sources' fingerprints from before the conversion (the delete
    # snapshot: size, mtime_ns, inode, device, by path): what a Referrer that
    # moves a Source checks it against before it does.
    source_fingerprints: Mapping[str, Mapping[str, int]] = field(
        default_factory=dict, compare=False,
    )
    # An Image renamed in place (#425), not a conversion: ``staged`` maps each
    # new name to the Image's file still under its old name, ``place`` renames
    # them all, and nothing is deleted. A Referrer that renames the file
    # itself (RomM) leaves ``is_placed`` true; the caller places what is left.
    rename: bool = False


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
    def apply(self, change: ReferrerChange) -> Applied | None:
        """Write *change* atomically with a one-time backup, or raise ReferrerError.

        Always synchronous. The hook calls it on its detached thread, unless
        the Referrer overrides :meth:`apply_async`. May return
        :class:`Applied` naming Source files it removed itself.
        """

    async def apply_async(self, change: ReferrerChange) -> Applied | None:
        """The async form of :meth:`apply`, for a Referrer whose apply waits
        on the network (the RomM swap, #391).

        A Referrer that overrides it is applied on the event loop: the hook
        awaits this, bounded by its bound and raced against Cancel (#383),
        instead of calling :meth:`apply` on its thread. It must never block
        the loop. By default it runs the synchronous :meth:`apply` on a worker
        thread, carrying the caller's context (so ``check_write`` sees the
        hook's guard).
        """
        return await asyncio.to_thread(self.apply, change)

    @classmethod
    def applies_async(cls) -> bool:
        """Whether this Referrer overrides :meth:`apply_async`, so the hook
        applies it on the event loop."""
        return cls.apply_async is not Referrer.apply_async

    def places_output(self, image_path: str, renames: Mapping[str, str]) -> bool:
        """Whether this Referrer, when on, moves a staged output into place
        itself (``StagedPlacement``), so the job keeps the output hidden until
        the hook runs (#391). Cheap: no I/O. False unless a Referrer says so."""
        return False

    def rename_refused(self, library) -> str | None:
        """Why an Image in *library* must not be renamed while this opt-in
        Referrer is off there, or None (#425). The registry asks it only of
        an opt-in Referrer the Library has off. Reads settings in memory."""
        return None


class WriteJournal:
    """Every Referrer file the hook rewrote, with its bytes before and after
    (#425): what an Image rename puts back when it doesn't go ahead.

    Kept apart from the hook's outcome on purpose: a phase the hook stops
    waiting for (a Cancel, its bound) returns no records, but its worker may
    already have written, and goes on recording here until it ends. Its writes
    are refused from the moment it is abandoned
    (``check_write``), so once every worker has ended (:meth:`wait_idle`)
    the journal is complete. Restoring writes back exactly the bytes that
    were there, never a re-plan, so an entry the rename didn't change is never
    touched. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[str, bytes, bytes]] = []
        self._running = 0
        self._idle = threading.Event()
        self._idle.set()

    def record(self, path: str, before: bytes, after: bytes) -> None:
        with self._lock:
            self._entries.append((path, before, after))

    def worker_started(self) -> None:
        with self._lock:
            self._running += 1
            self._idle.clear()

    def worker_ended(self) -> None:
        with self._lock:
            self._running -= 1
            if self._running <= 0:
                self._idle.set()

    def wait_idle(self, timeout: float) -> bool:
        """Block until no worker that records here is running, up to
        *timeout* seconds; whether none is."""
        return self._idle.wait(timeout)

    def written(self) -> list[tuple[str, bytes, bytes]]:
        """``(path, before, after)`` for each write, in the order written."""
        with self._lock:
            return list(self._entries)


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
    # The outputs still staged while the hook runs, when the job kept them
    # hidden for a Referrer that places them (#391). ``outputs`` then holds
    # their staged paths.
    placement: StagedPlacement | None = None
    # Where each file a Referrer rewrites is recorded, for a caller that may
    # put it back (an Image rename, #425). None: nothing is recorded.
    journal: WriteJournal | None = None

    def check(self, target: str) -> None:
        if self.abandoned.is_set():
            raise ReferrerError(target, "the job stopped waiting for it; not writing")
        for path, identity in self.outputs.items():
            if not same_identity(file_identity(path), identity):
                raise ReferrerError(
                    target, f"the output {os.path.basename(path)} changed after its check",
                )


# The guard ``check_write`` consults. A context variable, not thread-local
# state, so one mechanism covers both kinds of apply: the
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


def hook_running() -> bool:
    """Whether this runs inside the Referrer hook (planning or applying for a
    job), rather than for the delete confirmation."""
    return _guard.get() is not None


# The placement a plan made outside the hook is for (the rename preview, #425).
_planning: contextvars.ContextVar[StagedPlacement | None] = contextvars.ContextVar(
    "referrer_planning_placement", default=None,
)


@contextmanager
def planning(placement: StagedPlacement | None):
    """Plan, outside the hook, for *placement* (an Image rename's preview,
    #425): ``staged_placement`` answers it, as it will inside the hook."""
    token = _planning.set(placement)
    try:
        yield
    finally:
        _planning.reset(token)


def staged_placement() -> StagedPlacement | None:
    """The hook's staged outputs (``StagedPlacement``), or None when the job
    placed its output before the hook (#391). Outside the hook, the placement
    a plan is made for (``planning``), else None."""
    guard = _guard.get()
    return guard.placement if guard is not None else _planning.get()


def staged_output(final: str) -> str | None:
    """Where the output whose final path is *final* is still staged during
    the hook (``StagedPlacement``, #391), or None when it is in place, or
    outside the hook. *final* may be the resolved path. Blocking (it
    resolves paths): call it off the loop."""
    placement = staged_placement()
    if placement is None:
        return None
    for output, staged in placement.staged.items():
        if output == final or os.path.realpath(output) == final:
            return staged
    return None


def check_write(target: str) -> None:
    """Raise ReferrerError unless writing *target* is still allowed. Call it
    under the target's lock, right before each write. A no-op outside the hook
    (planning, or a caller that writes a new playlist).

    It stats each output, so an async apply on a slow mount calls it off the
    loop with its context (``asyncio.to_thread`` copies it)."""
    guard = _guard.get()
    if guard is not None:
        guard.check(target)


def record_write(target: str, before: bytes, after: bytes) -> None:
    """Note that *target* now holds *after* where it held *before*, in the
    hook's journal when it keeps one (``WriteGuard.journal``). Called right
    after the write landed, under the target's lock. A no-op otherwise."""
    guard = _guard.get()
    if guard is not None and guard.journal is not None:
        guard.journal.record(target, before, after)


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


def _hidden_slot(directory: str):
    """A staging slot (#398) beside *directory*'s files when a Library holding
    it hides in-progress files from its own scanner (``HIDDEN_STAGING``, RomM,
    whose folder may hold a folder Library, #514), else None. No such Library
    keeps the temp file beside the target as before. A slot that can't be made
    raises: the write never falls back to a temp file RomM could index."""
    from services import libraries, staging

    if not libraries.hidden_staging(directory):
        return None
    return staging.open_slot(directory)


@contextmanager
def _temp_file(directory: str):
    """A new hidden temp file for a write into *directory*: ``(fd, path)``.

    Its name is short and fixed-length (``.tmp-`` plus random characters),
    never built from the target's name, so a target near the 255-byte file
    name limit can still be written. It sits beside the target, except in a
    Library whose scanner indexes dot-prefixed files (RomM): there it is made
    in the hidden staging slot (a dot-prefixed folder on the same filesystem,
    so the rename stays atomic), which goes with the write."""
    slot = _hidden_slot(directory)
    try:
        yield short_temp_file(slot.path if slot is not None else directory, prefix=".tmp-")
    finally:
        if slot is not None:
            slot.close()


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
    with _temp_file(directory) as (fd, tmp):
        _write_replacing(fd, tmp, path, data, like, guard)


def _write_replacing(
    fd: int, tmp: str, path: str, data: bytes, like: str | None,
    guard: Callable[[], None] | None,
) -> None:
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
    with _temp_file(directory) as (fd, tmp):
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
