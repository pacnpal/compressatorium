"""Intake: one poll of an Inbox (#431).

The Rule scheduler's tick (``rules.sweep.run_forever``) calls :func:`poll_all`
once a minute, after the Library sweeps. Polled, never watched, so it works on
NFS and SMB shares. Each poll of an Inbox:

1. **Settles what finished.** A conversion intake queued that has ended:
   completed, its original goes to ``processed/``; failed or cancelled, the
   file stays with a Finding.
2. **Lists the Inbox**, stall-bounded: every Image file below its folder, a
   sheet with its tracks as one Image (``image_sets.group_images``). Never
   ``processed/``, a dot-entry (a staging folder), a link, or a file no tool
   reads or writes.
3. **Waits for each to settle.** An Image is taken once the size and mtime of
   every one of its files (``image_fingerprint``) are the same across two
   polls at least the Inbox's settle time apart. A download still growing is
   never touched.
4. **Takes what settled.** Its Platform comes from ``detect_platform``.
   Unsorted or conflicting, it stays with a Finding saying why. Otherwise the
   Library says where that Platform's Images go (``Library.placement``) and:

   * with an enabled conversion Rule for that Platform whose mode takes the
     file, a conversion is queued through the same job path a Rule sweep
     uses (``job_manager.create_batch_jobs``), into the placement folder, with
     ``verify_after`` so the output gets the Intact check. Its Ledger row,
     its slot in the concurrency limit and its Cancel are the queue's own.
     Nothing is ever deleted: ``delete_on_verify`` is never set;
   * with none, the Image is copied as it is, as an ``inbox_copy`` job you can
     cancel: into a hidden staging slot beside the placement folder
     (``services.staging``), read back and compared byte for byte, checked
     Intact by its own tool where one checks it, then placed without
     replacing anything. Each copy holds one of the same ``max_concurrent``
     slots conversions run in (``job_manager.hold_slot``).

   Once the output is in the Library the original (and its tracks) moves to
   ``processed/``, keeping its path in the Inbox. A name already taken there
   gets a numbered folder; nothing is replaced.

A file that got a Finding is held: it is not tried again until one of its
files changes, so a second poll changes nothing. Findings are written one file
at a time, under the Inbox's Library, keyed ``inbox:<id>/<path>``, and cleared
when the file is taken or gone.

Archives and names (#432):

* **An archive** (a zip or 7z) is unpacked before its Platform is worked
  out, as an ``inbox_unpack`` job holding a conversion slot: gated first
  (``ArchiveService.check_inbox_archive``: the Inbox's size and expansion
  caps, links, absolute and ``..`` paths), then every member checked again
  and written beneath a hidden staging slot beside it
  (``ArchiveService.extract_members`` into ``services.staging``). The Images
  in the slot are listed with the Inbox's own from then on and taken one by
  one like loose files; a Finding for one of them is written on the archive,
  naming it. Once every one of them is placed the archive moves to
  ``processed/`` and the slot, intake's own scratch, goes. An archive found
  inside an archive is not unpacked.
* **Recognizing.** Where a DAT (or Hasheous) can answer, each Image is
  matched before it is placed, as an ``inbox_recognize`` job: the match of
  the very bytes taken (``routes.dat.match_one``), never a row cached for an
  earlier file of that name, ranked by the current DAT ranking
  (``DATStore.with_current_ranking``, ``dat_ranking.ranked_match``).
* **Naming.** A Recognized Image's main file takes its top-ranked DAT name
  with its own extension, by the rename's rule (``dat_rename.dat_file_name``);
  a conversion's output takes it with the output's extension; a sheet's
  tracks keep theirs, so the sheet still names them. Two DATs the ranking
  puts level that name it differently hold it with an ``inbox_conflict``
  Finding naming both: it is never guessed at, and the hold lifts once the
  ranking changes.
* **A taken name.** A name already in the Library holds the Image with a
  Finding; a name only another job holds is tried again next poll.

Asking RomM to scan and the intake notification (#433) read what a poll
reports as ``placed``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionMode, JobStatus
from services import findings, platform_detect, staging
from services.archive import ArchiveSafetyRefused, archive_service
from services.dat_ranking import ranked_match
from services import inbox as inbox_store
from services.image_sets import group_images, image_fingerprint, image_members
from services.job_manager import OutputClaimedError, QueueBackpressureError, job_manager
from services.lock_manager import LockFailed, lock_manager
from services.output_conflicts import (
    QUEUE,
    SKIP_EXISTING,
    SKIP_LOCKED,
    output_status,
    resolve_destination,
)
from services.rules import check as checks
from services.rules.library import Library, LibraryUnreadable, PlacementUnknown
from services.rules.rules import CONVERT
from services.stream_digest import new_hasher
from services.subprocess_runner import (
    DeadlineExpired,
    MutationInFlight,
    MutationNotStarted,
    ReadCancelled,
    collect_abandonment,
    run_detached,
    run_detached_mutation,
    run_detached_undoable,
    run_detached_within_stall_bound,
)
from services.tools import registry
from utils.delete_plan import sheet_reference_problems
from utils.junk import is_junk_entry
from utils.path_utils import (
    MoveRollbackFailed,
    PlacementCommitted,
    file_snapshot,
    match_extension,
    move_anchored,
    open_dir_beneath,
    place_without_clobbering,
    stat_at,
    volume_containment,
)

logger = get_logger("inbox")

# How long one blocking step on a single file may take (a detection, a
# placement, a move) before the poll gives up on it for this tick.
_STEP_SECONDS = 60
# How long placing a checked copy may run before intake stops waiting on it
# with a bound and waits for its outcome instead: a rename can still land.
_PLACE_SECONDS = _STEP_SECONDS
# The same for moving an original to processed/.
_MOVE_SECONDS = _STEP_SECONDS
# How long taking a copy's output locks may take; one that answers later
# gives back what it took (`run_detached_undoable`).
_LOCK_SECONDS = _STEP_SECONDS
_PLACE_POLL_SECONDS = 0.05
# How long a listing may go without reading an entry before it is given up.
_LIST_STALL_SECONDS = 120
_CHUNK = 4 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


@dataclass
class _Seen:
    fingerprint: str
    since: float


@dataclass
class _Flight:
    """An Image intake has started on: a queued conversion or a running copy."""

    fingerprint: str
    files: list[str]
    platform: str
    # Each file's (device, inode, size, mtime) when it was taken, read through
    # the Inbox's own folders: what the copy reads and the move to
    # ``processed/`` takes must still be these files.
    identities: dict[str, dict[str, int]] = field(default_factory=dict)
    job_id: str | None = None
    task: asyncio.Task | None = None
    # Its output is in the Library and the originals still wait to go to
    # processed/: set when another job held one of them (Codex 4607108034),
    # so the next poll tries the move again (``_settle_flights``).
    archive: str | None = None
    # Each of its files' names in the Library, in ``files`` order: its main
    # file's DAT name (#432) or its own, its tracks' own.
    placed_names: list[str] = field(default_factory=list)
    # Its top-ranked DAT's game name (#432), or None to keep its own name.
    game_name: str | None = None
    # Set on the flight that moves an unpacked archive to processed/ once
    # every Image in it is placed (#432).
    unpacked: _Unpacked | None = None


@dataclass
class _Unpacked:
    """An archive intake unpacked (#432): its Images are taken one by one
    from the staging slot, and it moves to processed/ once all are placed."""

    fingerprint: str
    slot: Any  # services.staging.StagedOutput
    # The archive's own identity, for its move to processed/.
    identities: dict[str, dict[str, int]]
    images: frozenset[str]
    placed: set[str] = field(default_factory=set)


@dataclass
class _State:
    seen: dict[str, _Seen] = field(default_factory=dict)
    # Path -> the fingerprint it got a Finding (or a cancel) at. Not tried
    # again until it changes.
    held: dict[str, str] = field(default_factory=dict)
    # Path -> what an ownership hold depends on (the sheets that caused it,
    # each with its fingerprint): recomputed each poll, and a hold whose key
    # changed is lifted and the Image looked at again (Codex 4607108025).
    held_by: dict[str, tuple] = field(default_factory=dict)
    flights: dict[str, _Flight] = field(default_factory=dict)
    # What was placed since the last poll reported it: ``{"path", "platform",
    # "output"}``, for what runs after a pass (#433). An Image from an
    # archive (#432) is reported with the archive's path.
    placed: list[dict[str, Any]] = field(default_factory=list)
    # Archive path -> what was unpacked from it (#432).
    unpacked: dict[str, _Unpacked] = field(default_factory=dict)
    # Path -> (the fingerprint it was matched at, its match), #432.
    recognized: dict[str, tuple[str, dict]] = field(default_factory=dict)
    # Path -> the DAT ranking it was held at for a naming conflict (#432):
    # looked at again once the ranking changes.
    ranked_holds: dict[str, tuple] = field(default_factory=dict)
    last_poll: float | None = None
    waiting: int = 0
    error: str | None = None
    polling: bool = False
    # Set when the Inbox is removed (`forget`): a poll or copy still holding
    # this state admits nothing new (CodeRabbit 4606147955).
    revoked: bool = False
    # Guards `revoked` against a removal on another thread: set under it, and
    # each admission checks it and records what it admitted under it, with no
    # await between (CodeRabbit 4606443430). Never held across I/O or awaits.
    guard: threading.Lock = field(default_factory=threading.Lock)


_states: dict[int, _State] = {}
_tasks: set[asyncio.Task] = set()
# Set when a copy's check had to stop with a reader still on the storage
# (#306's stop): no copy opens another file until the next pass. Read and set
# under `_check_lock`, which every copy's check-and-place runs under, so a
# copy running beside the one that stops (max_concurrent > 1) sees it.
_storage_stop = False
# Guards `_storage_stop`, which a copy's staging (on a worker thread) reads
# right before it opens a file. Never held across I/O.
_stop_lock = threading.Lock()
_stop_generation = 0
_check_lock: asyncio.Lock | None = None
_check_lock_loop: asyncio.AbstractEventLoop | None = None


def _stopped() -> bool:
    with _stop_lock:
        return _storage_stop


def _set_stop(value: bool) -> None:
    global _storage_stop, _stop_generation
    with _stop_lock:
        _storage_stop = value
        if value:
            # Moves with every stop, so a check that raced one can tell.
            _stop_generation += 1


class _Deferred(Exception):
    """A copy that won't open another file this pass (a check had to stop)."""


def _may_open(name: str) -> int:
    """Right before a copy opens a file: raise :class:`_Deferred` when a
    check stopped with a reader still on the storage, else the stop
    generation the open goes ahead under (:func:`_opened`). Blocking-safe."""
    with _stop_lock:
        if _storage_stop:
            raise _Deferred(
                f"{name} isn't opened: an earlier check is still reading the storage",
            )
        return _stop_generation


def _opened(name: str, fd: int, generation: int) -> None:
    """Right after the open *generation* went ahead under: a stop that landed
    while it was in flight closes *fd* before a byte is read, and the copy
    ends deferred. The lock is never held across the open itself, which can
    stall on a mount that stopped answering."""
    with _stop_lock:
        raced = _storage_stop or _stop_generation != generation
    if raced:
        os.close(fd)
        raise _Deferred(f"{name} was opened as a check stopped; nothing was read from it")


def _checking() -> asyncio.Lock:
    """The check-and-place lock, made on the loop that first needs it."""
    global _check_lock, _check_lock_loop
    loop = asyncio.get_running_loop()
    if _check_lock is None or _check_lock_loop is not loop:
        _check_lock, _check_lock_loop = asyncio.Lock(), loop
    return _check_lock


def reset_for_tests() -> None:
    global _storage_stop, _check_lock, _check_lock_loop
    for state in _states.values():
        _close_unpacked(state)
    _states.clear()
    _tasks.clear()
    _storage_stop = False
    _check_lock = _check_lock_loop = None


def forget(inbox_id: int) -> None:
    """Drop a removed Inbox's intake state, revoking it: a poll still running
    on it starts no copy or conversion. A copy already running finishes.
    What was unpacked from its archives goes (intake's own scratch).
    Blocking: called from the Inbox's removal, off the event loop."""
    state = _states.pop(inbox_id, None)
    if state is not None:
        with state.guard:
            state.revoked = True
        _close_unpacked(state)


def _close_unpacked(state: _State) -> None:
    """Close every staging slot *state* unpacked archives into (idempotent).
    The map itself is left as it is: a poll still running on the loop may be
    reading it. Blocking."""
    for unpacked in list(state.unpacked.values()):
        unpacked.slot.close()


def _state(inbox_id: int) -> _State:
    return _states.setdefault(inbox_id, _State())


def status(inbox_id: int) -> dict[str, Any]:
    """An Inbox's intake as the Libraries view shows it."""
    state = _states.get(inbox_id) or _State()
    last = state.last_poll
    return {
        "last_poll": (
            datetime.fromtimestamp(last, timezone.utc).isoformat().replace("+00:00", "Z")
            if last is not None else None
        ),
        "waiting": state.waiting,
        "in_progress": len(state.flights),
        "held": len(state.held),
        "error": state.error,
    }


async def drain() -> None:
    """Wait for every copy intake started. For tests and shutdown."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _step(func, *args, **kwargs):
    """One blocking step, on a disposable thread, bounded."""
    return await asyncio.wait_for(run_detached(func, *args, **kwargs), _STEP_SECONDS)


# ---- listing -------------------------------------------------------------


class InboxUnavailable(Exception):
    """The Inbox folder isn't there, or can't be confirmed inside a Volume."""


def _image_extensions() -> tuple[str, ...]:
    from services.libraries.folder import _image_extensions as extensions

    return extensions()


def _list(
    folder: str, library: Library, progress, extra: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[str], dict[str, list[str]], dict[str, str]]:
    """Every Image in the Inbox at *folder* (path -> fingerprint), and the
    folders the walk couldn't list. Blocking.

    Skips ``processed/``, dot-entries, links and files no tool reads or
    writes (a numbered split part, ``Game.iso.0``, counts as its output's,
    as the folder Library lists it); walks no link. *extra*: the staging
    slots archives were unpacked into (#432), hidden folders inside the
    Inbox, walked too. Raises :class:`InboxUnavailable` when the folder
    isn't there, isn't confirmed inside a Volume, or now overlaps its
    Library (its root moved since the Inbox was saved): intake would read
    its own output.
    """
    if volume_containment(folder, treat_archives=False) is not True:
        raise InboxUnavailable(f"{folder} isn't confirmed inside a Volume")
    if not os.path.isdir(folder):
        raise InboxUnavailable(f"{folder} isn't there")
    if not library.root:
        raise InboxUnavailable(f"The Library {library.name!r} has no folder here")
    problem = inbox_store.overlap_problem(
        Path(folder).resolve(), Path(library.root).resolve(), library.name, own=True,
    )
    if problem:
        raise InboxUnavailable(problem)
    files, unlisted = _walk([folder, *extra], folder, progress)
    # Progress between Images through grouping, fingerprinting and the
    # ownership checks too, so the stall bound measures stalled storage, not
    # how long a big pass takes (Codex 4607108046).

    def members(path: str) -> list[str]:
        progress(path)
        return image_members(path)

    images: dict[str, str] = {}
    for path in group_images(sorted(files), members=members):
        progress(path)
        images[path] = image_fingerprint(path)
    return (
        images, unlisted, _sharing(images, progress),
        _behind_unreadable_sheets(images, progress, within=folder),
    )


def _walk(roots: list[str], inbox: str | None, progress) -> tuple[list[str], list[str]]:
    """The Image files below *roots* and the folders that couldn't be listed:
    no dot-entry, junk or link, no ``processed/`` at the top of the Inbox
    folder *inbox*. Blocking."""
    extensions = _image_extensions()
    files: list[str] = []
    unlisted: list[str] = []
    stack = list(roots)
    while stack:
        current = stack.pop()
        progress(current)
        try:
            listing = os.scandir(current)
        except OSError:
            unlisted.append(current)
            continue
        with listing:
            for entry in listing:
                progress()
                name = entry.name
                if name.startswith(".") or is_junk_entry(name):
                    continue
                if current == inbox and name == inbox_store.PROCESSED:
                    continue
                try:
                    if entry.is_symlink():
                        # Never taken nor walked; and not gone either: a
                        # Finding under it is kept.
                        unlisted.append(entry.path)
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if (
                    match_extension(name, extensions) is not None
                    or registry.split_owner(entry.path) is not None
                ):
                    files.append(entry.path)
    return files, unlisted


def _behind_unreadable_sheets(
    images: dict[str, str], progress=None, *, within: str | None = None,
) -> dict[str, str]:
    """Image -> the sheet of unknown ownership (its path) that could name it,
    for every loose track-type file in that sheet's folder or any folder
    below it (Codex 4606061269, 4607108018): a sheet may name
    ``tracks/Track.bin``. A sheet's ownership is unknown when it can't be
    read whole (unreadable, over the size cap) or its track list doesn't
    fully resolve beneath *within* (``sheet_track_list`` is None: an
    absolute, escaping or missing reference, anything ``image_members``
    drops, Codex 4607280415), the test ``owning_sheet`` applies. Such a
    sheet owns nobody, so its tracks would be taken as Images of their own:
    they are held instead, failing closed. Blocking."""
    from services.image_sets import SHEET_EXTENSIONS, is_playlist
    from utils.sheet_text import sheet_is_readable, sheet_track_list

    roots = [within] if within else None
    unreadable: dict[str, str] = {}
    for path in images:
        if progress is not None:
            progress(path)
        if not path.lower().endswith(SHEET_EXTENSIONS):
            continue
        if not sheet_is_readable(path) or sheet_track_list(path, within=roots) is None:
            unreadable.setdefault(os.path.dirname(path), path)

    def holder(path: str) -> str | None:
        folder = os.path.dirname(path)
        while True:
            if folder in unreadable:
                return unreadable[folder]
            parent = os.path.dirname(folder)
            if parent == folder:
                return None
            folder = parent

    held: dict[str, str] = {}
    for path in images:
        if path.lower().endswith(SHEET_EXTENSIONS) or is_playlist(path):
            continue
        sheet = holder(path)
        if sheet is not None:
            held[path] = sheet
    return held


def _sharing(images: dict[str, str], progress=None) -> dict[str, list[str]]:
    """The Images that share a file with another (two sheets naming one
    track): each one -> the others it shares with. Taking one in would move
    a file the other still needs to ``processed/``. Blocking (reads sheets)."""
    owners: dict[str, list[str]] = {}
    for path in images:
        if progress is not None:
            progress(path)
        for member in image_members(path):
            key = os.path.normcase(os.path.realpath(member))
            owners.setdefault(key, []).append(path)
    shared: dict[str, set[str]] = {}
    for paths in owners.values():
        if len(paths) > 1:
            for path in paths:
                shared.setdefault(path, set()).update(p for p in paths if p != path)
    return {path: sorted(others) for path, others in shared.items()}


# ---- Findings ------------------------------------------------------------


def _ownership_key(
    path: str, listing: dict[str, str], sharing: dict[str, list[str]],
    blocked: dict[str, str],
) -> tuple | None:
    """What an ownership hold on *path* depends on: the sheets sharing its
    files, or the unreadable sheet that may own it, each with its
    fingerprint now (None when it's gone). None when nothing holds it."""
    sheets = [*sharing.get(path, ()), *([blocked[path]] if path in blocked else [])]
    if not sheets:
        return None
    return tuple(sorted((sheet, listing.get(sheet)) for sheet in sheets))


async def _hold(
    inbox: inbox_store.Inbox, state: _State, path: str, fingerprint: str,
    kind: str, reason: str, cause: dict[str, Any] | None = None,
    *, depends: tuple | None = None,
) -> None:
    """Leave the file where it is with a Finding saying why, until it changes
    (or, for an ownership hold, until what it *depends* on changes)."""
    state.held[path] = fingerprint
    if depends is not None:
        state.held_by[path] = depends
    else:
        state.held_by.pop(path, None)
    state.ranked_holds.pop(path, None)
    state.seen.pop(path, None)
    shown, cause = path, dict(cause or {})
    owner = _unpacked_holding(state, path)
    if owner is not None:
        # An Image unpacked from an archive (#432): the Finding goes on the
        # archive, the file the user dropped, naming the Image.
        shown, unpacked = owner
        member = os.path.relpath(path, unpacked.slot.path)
        reason = f"{member} (unpacked from it): {reason}"
        cause["member"] = member
    item = findings.Observed(
        kind=kind, image_key=inbox.image_key(shown), path=shown, reason=reason,
        cause={"inbox": inbox.id, "fingerprint": fingerprint, **cause},
    )
    others = [k for k in findings.INBOX_KINDS if k != kind]

    def write() -> None:
        def current() -> bool:
            return inbox_store.get(inbox.id) is not None

        for other in others:
            findings.resolve(inbox.library_id, item.image_key, other, current=current)
        findings.observe(inbox.library_id, item, current=current)

    try:
        await _step(write)
    except Exception:
        logger.warning("inbox: could not record a Finding for %s", path, exc_info=True)


def _unpacked_holding(state: _State, path: str) -> tuple[str, _Unpacked] | None:
    """The archive whose staging slot *path* was unpacked into, and what was
    unpacked from it (#432); None for a file the user dropped."""
    for archive, unpacked in state.unpacked.items():
        if path.startswith(unpacked.slot.path + os.sep):
            return archive, unpacked
    return None


async def _clear(inbox: inbox_store.Inbox, path: str) -> None:
    key = inbox.image_key(path)

    def clear() -> None:
        for kind in findings.INBOX_KINDS:
            findings.resolve(inbox.library_id, key, kind)

    try:
        await _step(clear)
    except Exception:
        logger.warning("inbox: could not clear the Findings of %s", path, exc_info=True)


# ---- the poll ------------------------------------------------------------


async def poll_all(*, now: float | None = None) -> list[dict[str, Any]]:
    """One poll of every Inbox: a pass. Never raises; called from the
    scheduler's tick. A new pass may open files again after a check that had
    to stop (``_storage_stop``)."""
    _set_stop(False)
    try:
        inboxes = await run_in_threadpool(inbox_store.all_inboxes)
    except Exception:
        logger.warning("inbox: could not list the Inboxes", exc_info=True)
        return []
    results = []
    for inbox in inboxes:
        try:
            results.append(await poll(inbox, now=now))
        except Exception:
            logger.warning("inbox: polling %s failed", inbox.folder, exc_info=True)
    return results


async def poll(inbox: inbox_store.Inbox, *, now: float | None = None) -> dict[str, Any]:
    """One poll of *inbox* at *now* (seconds; the wall clock by default)."""
    from services import libraries

    now = time.time() if now is None else now
    state = _state(inbox.id)
    result: dict[str, Any] = {
        "inbox_id": inbox.id, "library_id": inbox.library_id,
        "queued": 0, "copying": 0, "held": 0, "waiting": 0,
        "placed": [], "error": None,
    }
    if state.polling:
        result["error"] = "poll_running"
        return result
    state.polling = True
    try:
        await _settle_flights(inbox, state)
        library = libraries.get(inbox.library_id)
        if library is None:
            state.error = result["error"] = "library_missing"
            return result
        try:
            listing, unlisted, sharing, blocked = await run_detached_within_stall_bound(
                _list, inbox.folder, library, what=f"The Inbox {inbox.folder}",
                stall=_LIST_STALL_SECONDS, report_progress=True,
                extra=tuple(u.slot.path for u in state.unpacked.values()),
            )
        except InboxUnavailable as exc:
            state.error = result["error"] = str(exc)
            return result
        except Exception as exc:
            logger.warning("inbox: listing %s failed", inbox.folder, exc_info=True)
            state.error = result["error"] = f"The Inbox couldn't be listed: {exc}"
            return result
        if state.revoked:
            result["error"] = "inbox_removed"
            return result
        state.error = None
        await _forget_gone(inbox, state, listing, unlisted)
        busy = await _settle_unpacked(inbox, state, listing, unlisted)
        stop = False
        ranking: tuple | None = None
        for path, fingerprint in listing.items():
            if state.revoked:
                break  # removed meanwhile: nothing more is taken
            if path in state.flights or path in busy:
                continue
            depends = _ownership_key(path, listing, sharing, blocked)
            if state.held.get(path) == fingerprint:
                if path in state.ranked_holds:
                    # Held for a naming conflict (#432): looked at again
                    # once the DAT ranking changed.
                    ranking = ranking or await _ranking_now()
                    if ranking is None or state.ranked_holds[path] == ranking:
                        continue
                elif path not in state.held_by or state.held_by[path] == depends:
                    continue
                # What held it changed (the other sheet gone, the unreadable
                # sheet removed or fixed): its stale Finding goes and it is
                # looked at again now, settled as it was.
                await _clear(inbox, path)
                state.seen[path] = _Seen(fingerprint, now - inbox.settle_seconds)
            state.held.pop(path, None)
            state.held_by.pop(path, None)
            state.ranked_holds.pop(path, None)
            seen = state.seen.get(path)
            if seen is None or seen.fingerprint != fingerprint:
                state.seen[path] = _Seen(fingerprint, now)
                result["waiting"] += 1
                continue
            if stop or now - seen.since < inbox.settle_seconds:
                result["waiting"] += 1
                continue
            if path in blocked:
                # A sheet beside it can't be read, so whether it is one of that
                # sheet's tracks can't be told: it isn't taken on its own.
                await _hold(
                    inbox, state, path, fingerprint, findings.INBOX_FAILED,
                    f"The track sheet {blocked[path]} can't be read, or names a track "
                    "it may not use (an absolute path, one outside its folder, or one "
                    "that isn't there), so it may be one of that sheet's tracks: fix or "
                    "remove the sheet first",
                    {"sheet": blocked[path]}, depends=depends,
                )
                result["held"] += 1
                continue
            if path in sharing:
                # Images that share a file are left alone, all of them:
                # archiving one would take a file the other still needs.
                others = ", ".join(os.path.basename(p) for p in sharing[path])
                await _hold(
                    inbox, state, path, fingerprint, findings.INBOX_FAILED,
                    f"It shares files with {others}, so it isn't taken in on its own: "
                    "take them in by hand, or give each its own copy of the files",
                    {"shares_with": sharing[path]}, depends=depends,
                )
                result["held"] += 1
                continue
            outcome = await _take(inbox, library, state, path, fingerprint)
            if outcome == "queue_full":
                stop = True
                result["waiting"] += 1
            elif outcome in ("queued", "copying", "held"):
                result[outcome] += 1
            else:
                result["waiting"] += 1
        state.waiting = result["waiting"]
        return result
    finally:
        state.last_poll = now
        result["placed"], state.placed = state.placed, []
        state.polling = False


async def _forget_gone(
    inbox: inbox_store.Inbox, state: _State, listing: dict, unlisted: list[str],
) -> None:
    """Forget files no longer in the Inbox, and clear their Findings.

    The stored Findings are reconciled against the listing, not only the held
    paths in memory: after a restart those are empty, and a Finding for a
    file removed meanwhile would otherwise stay for good. A Finding under a
    folder the walk couldn't list is kept: not seen is not gone.
    """
    for path in [p for p in state.seen if p not in listing]:
        state.seen.pop(path, None)
    for path in [p for p in state.held if p not in listing]:
        state.held.pop(path, None)
        state.ranked_holds.pop(path, None)
    for path in [p for p in state.recognized if p not in listing]:
        state.recognized.pop(path, None)
    keep = {inbox.image_key(p) for p in (*listing, *state.flights)}
    hidden = tuple(inbox.image_key(folder) for folder in unlisted)

    def reconcile() -> None:
        for kind in findings.INBOX_KINDS:
            for row in findings.stored_of(inbox.library_id, kind):
                key = row["image_key"]
                if not key.startswith(f"{inbox.key_prefix}/") or key in keep:
                    continue
                if any(key == h or key.startswith(f"{h}/") for h in hidden):
                    continue
                findings.resolve(inbox.library_id, key, kind)

    try:
        await _step(reconcile)
    except Exception:
        logger.warning("inbox: could not settle the Findings of %s", inbox.folder, exc_info=True)


async def _settle_unpacked(
    inbox: inbox_store.Inbox, state: _State, listing: dict, unlisted: list[str],
) -> set[str]:
    """Move each archive whose Images are all placed to ``processed/``, and
    let go of what was unpacked from one that is gone or changed (#432).
    Returns the paths this poll leaves alone: each archive being worked
    through, and each of its Images already placed."""
    busy: set[str] = set()
    for archive, unpacked in list(state.unpacked.items()):
        if archive in state.flights or any(i in state.flights for i in unpacked.images):
            busy.add(archive)
            busy.update(unpacked.placed)
            continue
        if archive not in listing and any(
            archive.startswith(folder + os.sep) for folder in unlisted
        ):
            busy.update(unpacked.placed)  # not seen is not gone
            continue
        if listing.get(archive) != unpacked.fingerprint:
            # Gone, or changed since it was unpacked: what was unpacked goes
            # (intake's own scratch; the archive still holds it). A changed
            # archive is unpacked again once it settles. Its Images, still
            # in this poll's listing, are left alone.
            busy.update(unpacked.images)
            state.unpacked.pop(archive, None)
            try:
                await _step(unpacked.slot.close)
            except (asyncio.TimeoutError, OSError):
                logger.warning("inbox: could not remove what was unpacked from %s", archive)
            continue
        busy.add(archive)
        busy.update(unpacked.placed)
        if unpacked.images <= unpacked.placed and state.held.get(archive) != unpacked.fingerprint:
            flight = _Flight(
                fingerprint=unpacked.fingerprint, files=[archive], platform="",
                identities=unpacked.identities, unpacked=unpacked,
            )
            _archive_later(inbox, state, archive, flight, None)
    return busy


async def _ranking_now() -> tuple | None:
    """The DAT ranking as it stands (each DAT and its rank), or None when it
    can't be read: what a naming conflict's hold waits on to change (#432)."""
    from services.dat_store import dat_store

    try:
        ranking = await _step(dat_store.get_ranking)
    except Exception:
        logger.warning("inbox: could not read the DAT ranking", exc_info=True)
        return None
    return tuple((dat["id"], dat["rank"]) for dat in ranking["dats"])


# ---- taking one Image ----------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    folder: str
    rule: dict | None
    reason: str = ""


async def _take(
    inbox: inbox_store.Inbox, library: Library, state: _State, path: str, fingerprint: str,
) -> str:
    """Start intake for one settled Image: ``queued``, ``copying``, ``held``,
    ``queue_full``, ``unpacking``, ``recognizing`` or ``retry`` (something
    transient; the next poll tries again)."""
    if archive_service.is_archive(path):
        # Unpacked before its Platform is worked out (#432).
        return await _take_archive(inbox, state, path, fingerprint)
    try:
        detection = await _step(platform_detect.detect_platform, path)
        members = await _step(image_members, path)
    except (asyncio.TimeoutError, OSError):
        return "retry"
    if not detection.decided:
        conflict = detection.how == platform_detect.CONFLICT
        kind = findings.INBOX_CONFLICT if conflict else findings.INBOX_UNSORTED
        what = "Its Platform is in doubt" if conflict else "Couldn't tell its Platform"
        await _hold(
            inbox, state, path, fingerprint, kind,
            f"{what}: {detection.reason}. Move it into a folder named after its "
            "Platform inside the Inbox.",
            {"how": detection.how, "header": detection.header, "folder": detection.folder},
        )
        return "held"
    platform = detection.platform
    files = [path, *members]
    try:
        sheet = await _step(sheet_reference_problems, path, within=[inbox.folder])
    except (asyncio.TimeoutError, OSError):
        return "retry"
    if sheet:
        # `image_members` drops a reference the confined resolver refuses
        # (``../tracks/Game.bin``), so the Image would look trackless and be
        # copied without its track. Held instead, saying which.
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            "Its track sheet can't be taken as it is: " + "; ".join(sheet[:3]),
            {"sheet": sheet[:10]},
        )
        return "held"
    problem = _files_problem(inbox, path, members)
    if problem:
        await _hold(inbox, state, path, fingerprint, findings.INBOX_FAILED, problem)
        return "held"
    try:
        identities = await _step(_identities, inbox, files)
    except asyncio.TimeoutError:
        return "retry"
    except OSError as exc:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"Its files can't be read inside the Inbox without following a link: {exc}",
        )
        return "held"
    try:
        plan = await _plan(library, platform, path)
    except PlacementUnknown as exc:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"Couldn't tell where {platform} Images go in {library.name}: {exc}",
            {"platform": platform},
        )
        return "held"
    except (LibraryUnreadable, asyncio.TimeoutError, OSError) as exc:
        logger.info("inbox: %s can't be placed now (%s); trying again later", path, exc)
        return "retry"
    if plan.reason:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED, plan.reason,
            {"platform": platform},
        )
        return "held"
    flight = _Flight(
        fingerprint=fingerprint, files=files, platform=platform, identities=identities,
    )
    outcome = await _name(inbox, state, path, flight)
    if outcome is not None:
        return outcome
    if plan.rule is not None:
        return await _convert(inbox, state, path, flight, plan)
    try:
        taken = await _step(_taken, plan.folder, flight.placed_names)
    except (asyncio.TimeoutError, OSError):
        return "retry"
    if taken:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"{taken} is already in the Library ({plan.folder})", {"platform": platform},
        )
        return "held"
    return _start_copy(inbox, state, path, flight, plan.folder)


# ---- Recognizing and naming (#432) -----------------------------------------


def _matching_available() -> bool:
    """Whether a DAT is loaded, or Hasheous can answer. Blocking."""
    from routes.dat import matching_available
    from services.dat_store import dat_store

    return matching_available(dat_store.has_dats())


async def _name(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight,
) -> str | None:
    """Name *flight* from its top-ranked DAT, Recognizing it first where a
    DAT can answer: None once it is named (``flight.game_name`` and
    ``flight.placed_names`` set; no name keeps its own), else the outcome
    :func:`_take` answers (``recognizing``, ``held`` or ``retry``)."""
    from services.dat_rename import conflict_text, dat_file_name
    from services.dat_store import dat_store

    names = [os.path.basename(f) for f in flight.files]
    flight.placed_names = names
    done = state.recognized.get(path)
    if done is None or done[0] != flight.fingerprint:
        try:
            if not await _step(_matching_available):
                return None  # no DAT loaded: it keeps its own name
        except Exception:
            logger.warning("inbox: could not tell whether a DAT is loaded", exc_info=True)
            return "retry"
        return _start_recognize(inbox, state, path, flight)
    # The ranking first, so a change made while this one is read lifts the
    # hold below on the next poll.
    ranking = await _ranking_now()
    try:
        payload = await _step(dat_store.with_current_ranking, done[1], path)
    except Exception:
        logger.warning("inbox: could not rank the match of %s", path, exc_info=True)
        return "retry"
    ranked = ranked_match(payload)
    if ranked.conflict:
        # Never guessed at: held, as the rename to DAT name leaves it
        # unnamed, until the ranking orders them.
        named, matches = conflict_text(ranked.conflict)
        await _hold(
            inbox, state, path, flight.fingerprint, findings.INBOX_CONFLICT,
            f"Its DAT name is in doubt: {named}, and the ranking puts them level; "
            "rank the DATs to choose",
            {"platform": flight.platform, "matches": matches},
        )
        if ranking is not None:
            state.ranked_holds[path] = ranking
        return "held"
    if ranked.top is not None and ranked.top.get("game_name"):
        named = dat_file_name(ranked.top["game_name"], names[0])
        if named:
            flight.game_name = ranked.top["game_name"]
            flight.placed_names = [named, *names[1:]]
    return None


def _start_recognize(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight,
) -> str:
    with state.guard:
        if state.revoked:
            return "retry"  # the Inbox was removed meanwhile
        _track(state, path, flight, _recognize(inbox, state, path, flight))
    return "recognizing"


async def _recognize(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight,
) -> None:
    """Match the Image against the DATs (and Hasheous), as an
    ``inbox_recognize`` job holding a conversion slot; the next poll names it
    from the result. A match that can't be made holds it with a Finding."""
    from routes.dat import match_one

    job = job_manager.create_external_job(
        os.path.basename(path), ConversionMode.INBOX_RECOGNIZE,
        message="Waiting to Recognize it",
    )
    cancel = job_manager.get_cancel_event(job.id)
    outcome, why, result = "failed", "", None
    try:
        if not await job_manager.hold_slot(job.id, cancel):
            outcome = "cancelled"
        else:
            try:
                await job_manager.update_external_job(job.id, message="Recognizing it")
                with collect_abandonment() as abandoned:
                    result, _cacheable = await match_one(path, cancel_event=cancel)
                if cancel is not None and cancel.is_set():
                    outcome = "cancelled"
                elif abandoned:
                    why = (
                        "matching left a process stuck on its storage "
                        f"({', '.join(abandoned)})"
                    )
                elif result.get("error"):
                    why = str(result["error"])
                else:
                    outcome = "recognized"
            finally:
                job_manager.release_slot(job.id)
    except Exception as exc:
        logger.warning("inbox: Recognizing %s failed", path, exc_info=True)
        why = str(exc)
    if outcome == "recognized":
        state.recognized[path] = (flight.fingerprint, result)
        await job_manager.finish_external_job(job.id, success=True)
        return
    if outcome == "cancelled":
        await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                    "Its intake was cancelled", {"platform": flight.platform})
        await job_manager.finish_external_job_cancelled(job.id, message="Cancelled")
        return
    await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                f"It couldn't be Recognized against the DATs: {why}",
                {"platform": flight.platform})
    await job_manager.finish_external_job(job.id, success=False, error_message=why)


# ---- unpacking an archive (#432) -------------------------------------------


async def _take_archive(
    inbox: inbox_store.Inbox, state: _State, path: str, fingerprint: str,
) -> str:
    """Start unpacking an archive, or hold it with a Finding saying why not."""
    if _unpacked_holding(state, path) is not None:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            "It's an archive inside an archive, which isn't unpacked: extract the "
            "outer one by hand",
        )
        return "held"
    # Anyone who can write the Inbox can drop one: an archive past the
    # Inbox's size and expansion caps is never unpacked (Codex 4607318277).
    try:
        await _step(archive_service.check_inbox_archive, path)
    except ArchiveSafetyRefused as exc:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"It's an archive the Inbox doesn't take: {exc.rule}. It wasn't unpacked; "
            "extract it, or raise the limit if you trust it",
            {"limit": exc.rule},
        )
        return "held"
    except asyncio.TimeoutError:
        return "retry"
    except Exception as exc:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"It couldn't be read as an archive ({exc}), so it wasn't unpacked",
        )
        return "held"
    try:
        identities = await _step(_identities, inbox, [path])
    except asyncio.TimeoutError:
        return "retry"
    except OSError as exc:
        await _hold(
            inbox, state, path, fingerprint, findings.INBOX_FAILED,
            f"It can't be read inside the Inbox without following a link: {exc}",
        )
        return "held"
    flight = _Flight(fingerprint=fingerprint, files=[path], platform="", identities=identities)
    with state.guard:
        if state.revoked:
            return "retry"  # the Inbox was removed meanwhile
        _track(state, path, flight, _unpack(inbox, state, path, flight))
    return "unpacking"


def _unpack_blocking(
    inbox: inbox_store.Inbox, path: str, flight: _Flight, progress,
) -> tuple[Any, list[str]]:
    """Unpack the archive at *path* into a fresh staging slot beside it: the
    open slot and the Images in it. The archive is read through the Inbox's
    own folder with no link followed, and only while it is the file that was
    taken. Blocking."""
    _may_open(os.path.basename(path))
    folder_fd, names = _folder_beneath(inbox, [path])
    slot = None
    try:
        name = names[0]
        generation = _may_open(name)
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=folder_fd)
        _opened(name, fd, generation)
        with os.fdopen(fd, "rb") as source:
            if file_snapshot(os.fstat(source.fileno())) != flight.identities.get(name):
                raise _SourceChanged(errno.ESTALE, f"{name} changed since it was taken")
            slot = staging.open_slot(os.path.dirname(path))
            archive_service.extract_members(path, slot.path, source=source, progress=progress)
            if file_snapshot(os.fstat(source.fileno())) != flight.identities.get(name):
                raise _SourceChanged(errno.ESTALE, f"{name} changed while it was unpacked")
        files, _unlisted = _walk([slot.path], None, progress)
        images = list(group_images(sorted(files)))
        progress()
        return slot, images
    except BaseException:
        if slot is not None:
            slot.close()
        raise
    finally:
        os.close(folder_fd)


async def _unpack(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight,
) -> None:
    """Unpack one archive as an ``inbox_unpack`` job you can cancel, holding
    one of the ``max_concurrent`` slots; its Images are taken from the next
    poll on."""
    job = job_manager.create_external_job(
        os.path.basename(path), ConversionMode.INBOX_UNPACK, message="Waiting to unpack it",
    )
    cancel = job_manager.get_cancel_event(job.id)
    outcome, why, unpacked = "failed", "", None
    try:
        if not await job_manager.hold_slot(job.id, cancel):
            outcome = "cancelled"
        else:
            try:
                if _stopped():
                    outcome = "deferred"
                else:
                    outcome, why, unpacked = await _unpack_in(job.id, inbox, path, flight, cancel)
            finally:
                job_manager.release_slot(job.id)
    except Exception as exc:
        logger.warning("inbox: unpacking %s failed", path, exc_info=True)
        outcome, why = "failed", str(exc)
    if outcome == "unpacked":
        slot, images = unpacked
        with state.guard:
            revoked = state.revoked
            if not revoked and images:
                state.unpacked[path] = _Unpacked(
                    fingerprint=flight.fingerprint, slot=slot,
                    identities=flight.identities, images=frozenset(images),
                )
        if revoked or not images:
            try:
                await _step(slot.close)
            except (asyncio.TimeoutError, OSError):
                logger.warning("inbox: could not remove what was unpacked from %s", path)
        if not revoked and not images:
            why = "it holds no Image the app reads"
            await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                        f"Nothing was taken from it: {why}")
            await job_manager.finish_external_job(job.id, success=False, error_message=why)
            return
        await job_manager.finish_external_job(job.id, success=True)
        return
    if outcome == "cancelled":
        await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                    "Its intake was cancelled")
        await job_manager.finish_external_job_cancelled(job.id, message="Cancelled")
        return
    if outcome in ("changed", "deferred"):
        # Not a failure of the file: it is left to settle and be taken again.
        state.seen.pop(path, None)
        why = why or "the storage is still being read by an earlier check"
        await job_manager.finish_external_job(
            job.id, success=False, error_message=f"{why}; it is taken again once it settles",
        )
        return
    if outcome == "refused":
        message = (
            f"It's an archive the Inbox doesn't take: {why}. It wasn't unpacked; "
            "extract it by hand if you trust it"
        )
    else:
        message = f"Unpacking it failed: {why}"
    await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED, message)
    await job_manager.finish_external_job(job.id, success=False, error_message=why)


async def _unpack_in(
    job_id: str, inbox: inbox_store.Inbox, path: str, flight: _Flight,
    cancel: asyncio.Event | None,
) -> tuple[str, str, tuple[Any, list[str]] | None]:
    """Unpack, stall-bounded: ``(outcome, why, (slot, Images))``."""
    await job_manager.update_external_job(job_id, message="Unpacking it")
    handoff = _SlotHandoff(_unpack_blocking)
    try:
        unpacked = await run_detached_within_stall_bound(
            handoff.stage, inbox, path, flight, what=f"Unpacking {os.path.basename(path)}",
            cancel_event=cancel, report_progress=True,
        )
    except BaseException as exc:
        # Given up on: a slot the worker delivers now, or already did, is closed.
        handoff.abandon()
        if isinstance(exc, ReadCancelled):
            return "cancelled", "", None
        if isinstance(exc, _Deferred):
            return "deferred", str(exc), None
        if isinstance(exc, _SourceChanged):
            return "changed", str(exc), None
        if isinstance(exc, ArchiveSafetyRefused):
            return "refused", exc.rule, None
        if isinstance(exc, Exception):
            return "failed", str(exc), None
        raise
    handoff.taken()
    return "unpacked", "", unpacked


def _taken(folder: str, names: list[str]) -> str | None:
    """The first of *names* already in *folder*, or None. Blocking.
    Checked before a copy starts, so a taken name costs no copy; the
    placement itself never replaces either."""
    for name in names:
        if os.path.lexists(os.path.join(folder, name)):
            return name
    return None


def _files_problem(inbox: inbox_store.Inbox, path: str, members: list[str]) -> str | None:
    """Why the Image's files can't be moved together, or None. Its tracks must
    sit beside it in the Inbox, never in ``processed/``."""
    folder = os.path.dirname(path)
    for member in members:
        if os.path.dirname(os.path.normpath(member)) != folder:
            return (
                f"{os.path.basename(member)}, a file of this Image, isn't in the same "
                "folder, so the Image can't be moved as one"
            )
    return None


async def _plan(library: Library, platform: str, path: str) -> _Plan:
    """Where *platform*'s Images go in *library*, and the enabled Rule that
    converts *path* there, if any. Raises PlacementUnknown or LibraryUnreadable."""
    from services.rules import sweep

    rules = await sweep.get_rules(library, CONVERT)
    listed = None
    platform_id = library.canonical_platform_id(platform)
    rule = rules.get(platform_id) if platform_id is not None else None
    if platform_id is None and rules:
        # The Library names Platforms its own way (RomM's ids): find the one
        # with this slug among its Platforms.
        listed = await _step(library.platforms)
        for each in listed:
            slug = platform_detect.canonical_slug(each.slug or each.id)
            if slug == platform and each.id in rules:
                rule = rules[each.id]
                break
    placement = await _step(library.placement, platform, known=listed)
    if rule is None or not rule.get("enabled"):
        return _Plan(folder=placement.folder, rule=None)
    try:
        spec = registry.spec(rule["mode"])
        tool = registry.for_mode(rule["mode"])
    except KeyError:
        return _Plan(placement.folder, None, f"The {platform} Rule's mode {rule['mode']} is gone")
    if not registry.mode_allows_platform(rule["mode"], platform):
        return _Plan(
            placement.folder, None,
            f"The {platform} Rule converts with {rule['mode']}, which isn't for {platform}",
        )
    if not await _step(sweep._accepts_source, tool, spec, path):
        # Already in a form the Rule doesn't convert (the target format):
        # it goes in as it is.
        return _Plan(folder=placement.folder, rule=None)
    if not await registry.tool_is_ready(tool):
        return _Plan(placement.folder, None, f"{spec.tool_id} isn't ready, so the Rule can't run")
    return _Plan(folder=placement.folder, rule=rule)


def _destination(
    path: str, rule: dict, folder: str, game_name: str | None = None,
) -> tuple[str | None, str, str]:
    """The conversion's output in *folder*, made if it isn't there yet,
    whether it's free, and its name. With *game_name* (#432) the output
    takes that DAT name, with its own extension, by the rename's rule.
    Blocking."""
    from services.dat_rename import dat_file_name

    if volume_containment(folder) is not True:
        return None, "the placement folder isn't confirmed inside a Volume", ""
    os.makedirs(folder, exist_ok=True)
    mode = rule["mode"]
    tool = registry.for_mode(mode)
    try:
        own = tool.output_path(mode, path, folder)
    except (KeyError, ValueError, OSError):
        own = None
    named = dat_file_name(game_name, os.path.basename(own)) if own and game_name else None
    if named:
        destination = os.path.join(os.path.dirname(own), named)
        on_disk, locked = output_status(mode, destination)
        decision = SKIP_EXISTING if on_disk else SKIP_LOCKED if locked else QUEUE
    else:
        destination, decision = resolve_destination(tool, path, mode, folder, "skip")
    shown = named or (os.path.basename(own) if own else "")
    if decision == SKIP_LOCKED:
        # Another job holds the name, nothing is on disk: busy for now, never
        # a Finding (Codex 4606794174).
        return None, "busy", shown
    if decision != QUEUE or not destination:
        return None, "already_taken", shown
    return destination, QUEUE, shown


async def _convert(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, plan: _Plan,
) -> str:
    """Queue the Rule's conversion into the placement folder."""
    from services.rules import sweep

    rule = plan.rule
    fingerprint, platform = flight.fingerprint, flight.platform
    try:
        destination, decision, shown = await _step(
            _destination, path, rule, plan.folder, flight.game_name,
        )
    except (asyncio.TimeoutError, OSError):
        return "retry"
    if state.revoked:
        return "retry"  # the Inbox was removed meanwhile
    if decision == "busy":
        return "retry"  # taken again next poll, its fingerprint not held
    if destination is None:
        reason = (
            f"{shown or 'Its output'} is already in the Library ({plan.folder})"
            if decision == "already_taken" else f"Can't convert it: {decision}"
        )
        await _hold(inbox, state, path, fingerprint, findings.INBOX_FAILED, reason,
                    {"platform": platform})
        return "held"
    try:
        jobs = await job_manager.create_batch_jobs(
            [path], ConversionMode(rule["mode"]),
            output_dir=plan.folder, compression=sweep._compression_arg(rule),
            split=rule["split"],
            # The Intact check wherever the mode can check its output; the
            # registry says which (a WUD from jwud_decompress can't be).
            verify_after=registry.mode_supports_verify(rule["mode"]),
            output_paths={path: destination},
            # The file taken, and the Inbox it must stay under: the job checks
            # both, under its Source lock, before its tool reads anything, so a
            # folder swapped for a link meanwhile converts nothing.
            source_identities={path: {
                "root": inbox.folder,
                "snapshot": flight.identities[os.path.basename(path)],
                # Its tracks too: the job binds every file it reads.
                "members": {
                    name: snapshot for name, snapshot in flight.identities.items()
                    if name != os.path.basename(path)
                },
            }},
        )
    except QueueBackpressureError:
        return "queue_full"
    except OutputClaimedError:
        return "retry"
    except Exception as exc:
        logger.warning("inbox: could not queue %s", path, exc_info=True)
        await _hold(inbox, state, path, fingerprint, findings.INBOX_FAILED,
                    f"Couldn't queue its conversion: {exc}", {"platform": platform})
        return "held"
    with state.guard:
        revoked = state.revoked
        if not revoked:
            job = jobs[0]
            job.platform = platform
            flight.job_id = job.id
            state.flights[path] = flight
    if revoked:
        # Removed while the jobs were being made: they are cancelled here,
        # before this task yields to the dispatcher, so none starts; the
        # Source is left as it is.
        for each in jobs:
            await job_manager.cancel_job(each.id)
        return "retry"
    return "queued"


async def _settle_flights(inbox: inbox_store.Inbox, state: _State) -> None:
    """Finish the conversions intake queued that have ended."""
    for path, flight in list(state.flights.items()):
        if flight.job_id is None:
            if flight.archive is not None and (flight.task is None or flight.task.done()):
                # Its archival was deferred: tried again now.
                _archive_later(inbox, state, path, flight, flight.archive)
            continue  # a copy, or an archival, which finishes itself
        job = job_manager.get_job_for_lookup(flight.job_id)
        if job is None:
            state.flights.pop(path, None)
            await _hold(
                inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                "Lost track of its conversion; check the Library before taking it again",
                {"platform": flight.platform},
            )
            continue
        if job.status == JobStatus.COMPLETED:
            # Moving the original to processed/ is its own tracked flight
            # (CodeRabbit 4606147968): a slow move never holds up this poll,
            # and the Image stays in flight, not taken again, until it ends.
            flight.job_id = None
            _archive_later(inbox, state, path, flight, job.output_path)
        elif job.status == JobStatus.FAILED:
            state.flights.pop(path, None)
            await _hold(
                inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                f"Its conversion failed: {job.error_message or 'see the job'}",
                {"platform": flight.platform},
            )
        elif job.status == JobStatus.CANCELLED:
            state.flights.pop(path, None)
            await _hold(
                inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                "Its intake was cancelled", {"platform": flight.platform},
            )


def _archive_later(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, output: str | None,
) -> None:
    """Move the originals to ``processed/`` as *flight*'s own tracked task
    (CodeRabbit 4606147968): a slow move never holds up a poll."""
    flight.archive = None
    _track(state, path, flight, _finish(inbox, state, path, flight, output))


async def _lock_all(paths: list[str], *, allow_existing: bool) -> str | None:
    """The lock manager's exclusive locks on every one of *paths*, or none
    (``LockManager.acquire_all``, the path a conversion's locks take): None
    once all are held, else the path another job holds. Bounded; an
    acquisition that finishes after its wait gave up gives back what it took.
    Raises ``DeadlineExpired``, ``TimeoutError``, ``OSError``, ``LockFailed``."""

    def give_back(result: str | None) -> None:
        if result is None:
            _release_locks(paths)

    return await run_detached_undoable(
        lock_manager.acquire_all, [(path, allow_existing) for path in paths],
        timeout=_LOCK_SECONDS, undo=give_back,
    )


async def _finish(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, output: str | None,
) -> None:
    """The output is in the Library: move the original to ``processed/``.

    Only while it is still the Image that was taken: replaced or changed since,
    the Library holds the old contents, so the file stays in the Inbox to
    settle and be taken again. And only under the exclusive locks on every
    original (Codex 4607108034): while another job reads one (its input
    lock), nothing moves, no Finding is written, and the Image stays in
    flight for the next poll (``flight.archive``).
    """
    try:
        refused = await _lock_all(flight.files, allow_existing=True)
    except DeadlineExpired:
        refused = "(no answer)"
    except (OSError, asyncio.TimeoutError, LockFailed) as exc:
        await _hold(
            inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
            f"It's in the Library, but its files couldn't be locked to move them "
            f"to processed/: {exc}", {"platform": flight.platform},
        )
        return
    if refused is not None:
        logger.info("inbox: %s is in use (%s); moving it to processed/ later", path, refused)
        flight.archive = output or ""
        return
    try:
        await _finish_locked(inbox, state, path, flight, output)
    finally:
        try:
            await _step(_release_locks, flight.files)
        except (asyncio.TimeoutError, OSError):
            logger.warning("inbox: could not release the locks of %s", path, exc_info=True)


async def _finish_locked(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, output: str | None,
) -> None:
    """:func:`_finish`, its locks held. An Image unpacked from an archive
    (#432) stays in its slot, counted placed; the archive moves once all of
    them are (``flight.unpacked``), and its slot goes then."""
    owner = _unpacked_holding(state, path)
    try:
        same = await _step(image_fingerprint, path) == flight.fingerprint
        if same and owner is None:
            # A move past its bound may still land: its outcome is waited for
            # (`_owned`), so a move that lands late is counted, not lost.
            await _owned(
                _move_to_processed, inbox, flight.files, flight.identities,
                timeout=_MOVE_SECONDS, what=f"moving {os.path.basename(path)} to processed/",
            )
    except _SourceChanged:
        same = False
    except (OSError, asyncio.TimeoutError, MutationNotStarted) as exc:
        await _hold(
            inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
            f"It's in the Library, but couldn't be moved to processed/: {exc}",
            {"platform": flight.platform},
        )
        return
    if not same:
        logger.info("inbox: %s changed after it was placed; leaving it to settle again", path)
        state.seen.pop(path, None)
        return
    state.seen.pop(path, None)
    state.held.pop(path, None)
    state.recognized.pop(path, None)
    if flight.unpacked is not None:
        # The archive is in processed/: what was unpacked from it goes. Its
        # Images were counted and reported as each was placed.
        if state.unpacked.get(path) is flight.unpacked:
            state.unpacked.pop(path, None)
        try:
            await _step(flight.unpacked.slot.close)
        except (asyncio.TimeoutError, OSError):
            logger.warning("inbox: could not remove what was unpacked from %s", path)
        await _clear(inbox, path)
        return
    if owner is not None:
        owner[1].placed.add(path)
    # An Image from an archive is reported with the archive's path.
    state.placed.append({
        "path": owner[0] if owner else path, "platform": flight.platform, "output": output,
    })
    try:
        await _step(inbox_store.note_processed, inbox.id)
    except Exception:
        logger.warning("inbox: could not count %s as processed", path, exc_info=True)
    if owner is None:
        # An archive's Findings stay until it moves: another of its Images
        # may still be held.
        await _clear(inbox, path)


class _SourceChanged(OSError):
    """A file isn't the one intake took (replaced, changed or gone)."""


def _folder_beneath(inbox: inbox_store.Inbox, files: list[str]) -> tuple[int, list[str]]:
    """The Image's folder in the Inbox, opened beneath the Inbox's own folder
    with no link followed on the way (``open_dir_beneath``), and the files'
    names in it. Every later look at the files goes through the descriptor,
    so a folder swapped for a link afterwards can't redirect it. Raises
    OSError; the caller closes the descriptor. Blocking."""
    rel = os.path.relpath(os.path.dirname(files[0]), inbox.folder)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise OSError(errno.EACCES, f"{files[0]} isn't in the Inbox")
    fd = open_dir_beneath(inbox.folder, "" if rel == "." else rel.replace(os.sep, "/"))
    return fd, [os.path.basename(f) for f in files]


def _identities(inbox: inbox_store.Inbox, files: list[str]) -> dict[str, dict[str, int]]:
    """Each file's identity, read through the Inbox's own folders. Each must
    be a regular file. Blocking."""
    fd, names = _folder_beneath(inbox, files)
    try:
        out = {}
        for name in names:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, f"{name} isn't a regular file")
            out[name] = file_snapshot(st)
        return out
    finally:
        os.close(fd)


def _still_taken(inbox: inbox_store.Inbox, path: str, flight: _Flight) -> None:
    """Raise :class:`_SourceChanged` unless the Image at *path* is still the
    one taken: each file's identity through the Inbox's own folder, and the
    whole Image's fingerprint (its sheet, its tracks). Blocking."""
    fd, _names = _folder_beneath(inbox, flight.files)
    try:
        _check_identities(fd, flight.identities)
    finally:
        os.close(fd)
    if image_fingerprint(path) != flight.fingerprint:
        raise _SourceChanged(errno.ESTALE, f"{os.path.basename(path)} changed since it was taken")


def _check_identities(fd: int, identities: dict[str, dict[str, int]]) -> None:
    """Raise :class:`_SourceChanged` unless every file in the folder *fd* is
    still the one that was taken."""
    for name, taken in identities.items():
        st = stat_at(name, fd)
        if st is None:
            raise _SourceChanged(errno.ENOENT, f"{name} is gone")
        if not stat.S_ISREG(st.st_mode) or file_snapshot(st) != taken:
            raise _SourceChanged(errno.ESTALE, f"{name} changed since it was taken")


def _dir_at(parent: int, name: str, *, make: bool) -> int:
    """The folder *name* in *parent*, made if asked, opened with no link followed."""
    if make:
        try:
            os.mkdir(name, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _exists_at(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _processed_folder(
    inbox: inbox_store.Inbox, files: list[str], names: list[str],
) -> tuple[int, str]:
    """``processed/<the Image's folder in the Inbox>``, or a numbered
    ``<stem> (n)`` folder in it when one of *names* is taken there, made and
    opened beneath the Inbox with no link followed: its descriptor and its
    path (for messages). Blocking."""
    rel = os.path.relpath(os.path.dirname(files[0]), inbox.folder)
    parts = [inbox_store.PROCESSED, *([] if rel == "." else rel.split(os.sep))]
    where = os.path.join(inbox.folder, *parts)
    fd = open_dir_beneath(inbox.folder)
    try:
        for part in parts:
            child = _dir_at(fd, part, make=True)
            os.close(fd)
            fd = child
        if not any(_exists_at(fd, name) for name in names):
            return fd, where
        stem = os.path.splitext(names[0])[0]
        for n in range(2, 1000):
            numbered = f"{stem} ({n})"
            child = _dir_at(fd, numbered, make=not _exists_at(fd, numbered))
            if any(_exists_at(child, name) for name in names):
                os.close(child)
                continue
            os.close(fd)
            return child, os.path.join(where, numbered)
        raise OSError(errno.EEXIST, "No free name in processed/")
    except BaseException:
        os.close(fd)
        raise


def _move_to_processed(
    inbox: inbox_store.Inbox, files: list[str],
    identities: dict[str, dict[str, int]],
) -> None:
    """Move an Image's *files* into ``processed/``, keeping their folder in the
    Inbox. Anchored, as the Image rename is: both folders are opened beneath
    the Inbox with no link followed, each file must still be the one taken
    (*identities*; else :class:`_SourceChanged` and nothing moves), and each
    move is a no-replace rename between the two descriptors
    (``place_without_clobbering`` with ``src_dir_fd``/``dst_dir_fd``). All or
    none: a move that fails puts back the ones already moved. Blocking."""
    if volume_containment(inbox.folder) is not True:
        raise OSError(errno.EACCES, "The Inbox isn't confirmed inside a Volume")
    src, names = _folder_beneath(inbox, files)
    try:
        _check_identities(src, identities)
        dst, processed = _processed_folder(inbox, files, names)
        try:
            moved: list[str] = []
            try:
                for name in names:
                    # The shared anchored move (#425's too): only while it is
                    # the file taken, never over anything, and the entry that
                    # landed checked again (one swapped in is moved back).
                    try:
                        move_anchored(name, src, dst, identities[name])
                    except PlacementCommitted:
                        moved.append(name)  # it landed: put back with the rest
                        raise
                    except MoveRollbackFailed:
                        raise  # reported as it is, never as a changed file
                    except OSError as exc:
                        if exc.errno == errno.ESTALE:
                            raise _SourceChanged(errno.ESTALE, str(exc)) from exc
                        raise
                    moved.append(name)
            except OSError as failure:
                # Back the way they came, through the same two folders.
                stuck = []
                inbox_folder = os.path.dirname(files[0])
                for name in reversed(moved):
                    here = os.path.join(processed, name)
                    there = os.path.join(inbox_folder, name)
                    at_old, at_new = stat_at(name, src), stat_at(name, dst)
                    if at_old is not None and at_new is not None and (
                        at_old.st_dev, at_old.st_ino,
                    ) == (at_new.st_dev, at_new.st_ino):
                        # Both names of one file (a move that never let go
                        # of its Inbox name): the processed/ name goes.
                        try:
                            os.unlink(name, dir_fd=dst)
                        except OSError as exc:
                            stuck.append(f"{here} (a second name of {there}; {exc})")
                        continue
                    if at_old is not None:
                        # Its Inbox name is taken by another file (a writer
                        # put one there): never moved back over it. It keeps
                        # its only name, in processed/ (CodeRabbit 4607070950).
                        logger.error(
                            "inbox: %s stays in processed/: %s is taken by another file",
                            here, there,
                        )
                        stuck.append(f"{here} (its name in the Inbox, {there}, is taken by "
                                     "another file, which was left alone)")
                        continue
                    try:
                        place_without_clobbering(
                            name, name, src_dir_fd=dst, dst_dir_fd=src, identity_sensitive=True,
                        )
                    except OSError as exc:
                        logger.error("inbox: could not put %s back in the Inbox: %s", name, exc)
                        stuck.append(f"{here} (couldn't be put back as {there}: {exc})")
                if stuck:
                    raise MoveRollbackFailed(
                        errno.EIO,
                        f"moving it to processed/ failed ({failure}), and these stay in "
                        f"processed/: {'; '.join(stuck)}",
                    ) from failure
                raise
        finally:
            os.close(dst)
    finally:
        os.close(src)


# ---- copying an Image in as it is ----------------------------------------


def _start_copy(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, folder: str,
) -> str:
    with state.guard:
        # The check and the scheduling as one step: a removal waits for it.
        if state.revoked:
            return "retry"  # the Inbox was removed meanwhile: no copy starts
        _track(state, path, flight, _copy(inbox, state, path, flight, folder))
    return "copying"


def _track(state: _State, path: str, flight: _Flight, work: Any) -> None:
    """Run *work* (a copy, or the move after a conversion) as *flight*'s
    task, *path* in flight until it ends; :func:`drain` waits for it."""

    async def run() -> None:
        try:
            await work
        finally:
            # A deferred archival keeps the Image in flight for the next poll.
            if state.flights.get(path) is flight and flight.archive is None:
                state.flights.pop(path, None)

    task = asyncio.ensure_future(run())
    flight.task = task
    state.flights[path] = flight
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class _CopyDiffers(Exception):
    pass


def _stage_copy(
    inbox: inbox_store.Inbox, flight: _Flight, folder: str, progress,
) -> tuple[Any, list[str]]:
    """Copy the Image's files into a fresh staging slot beside *folder*'s
    files, each read through the Inbox's own folder (no link followed) and
    only while it is still the file that was taken, then read back and
    compared. Returns the open slot and the names they are placed under.
    Blocking."""
    _may_open(os.path.basename(flight.files[0]))
    folder_fd, names = _folder_beneath(inbox, flight.files)
    # Each file under its name in the Library (a DAT name, #432).
    placed = flight.placed_names or names
    slot = None
    try:
        slot = staging.open_slot(folder, make_folder=True)
        for name, as_name in zip(names, placed):
            generation = _may_open(name)
            target = slot.path_for(as_name)
            wrote = new_hasher("sha1")
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=folder_fd)
            _opened(name, fd, generation)
            try:
                if file_snapshot(os.fstat(fd)) != flight.identities.get(name):
                    raise _SourceChanged(errno.ESTALE, f"{name} changed since it was taken")
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "rb") as src, open(target, "xb") as dst:
                while chunk := src.read(_CHUNK):
                    progress(name)
                    wrote.update(chunk)
                    dst.write(chunk)
                # The file read is still the one taken after its last read
                # (written to in place meanwhile, its mtime moved).
                if file_snapshot(os.fstat(src.fileno())) != flight.identities.get(name):
                    raise _SourceChanged(errno.ESTALE, f"{name} changed while it was copied")
                dst.flush()
                os.fsync(dst.fileno())
            read = new_hasher("sha1")
            # The read-back is an open like any other: guarded by the same
            # stop check right before it and right after it.
            generation = _may_open(name)
            back_fd = os.open(target, os.O_RDONLY | _NOFOLLOW)
            _opened(name, back_fd, generation)
            with os.fdopen(back_fd, "rb") as back:
                while chunk := back.read(_CHUNK):
                    progress(target)
                    read.update(chunk)
            if read.hexdigest() != wrote.hexdigest():
                raise _CopyDiffers(f"the copy of {name} doesn't read back the same")
        # A source rewritten in place while it was read gives a copy that reads
        # back as written yet is neither version: only a source unchanged from
        # before the first read to after the last is placed.
        _check_identities(folder_fd, flight.identities)
        # An awaiter that gave up meanwhile makes this raise, so the slot is
        # closed below instead of left open with nobody to close it.
        progress()
        return slot, list(placed)
    except BaseException:
        if slot is not None:
            slot.close()
        raise
    finally:
        os.close(folder_fd)


async def _copy(
    inbox: inbox_store.Inbox, state: _State, path: str, flight: _Flight, folder: str,
) -> None:
    """Copy one Image into the Library as it is, as a job you can cancel."""
    job = job_manager.create_external_job(
        os.path.basename(path), ConversionMode.INBOX_COPY,
        message=f"Waiting to copy into {folder}",
    )
    cancel = job_manager.get_cancel_event(job.id)
    outcome, why, output = "failed", "", None
    try:
        # One of the same max_concurrent slots conversions run in (#431),
        # given back on every outcome.
        if not await job_manager.hold_slot(job.id, cancel):
            outcome = "cancelled"
        else:
            try:
                if _stopped():
                    outcome = "deferred"
                else:
                    outcome, why, output = await _copy_in(
                        job.id, inbox, path, flight, folder, cancel,
                    )
            finally:
                job_manager.release_slot(job.id)
    except Exception as exc:
        logger.warning("inbox: copying %s failed", path, exc_info=True)
        outcome, why = "failed", str(exc)
    # The Image stays in flight until this ends (`_track`), its move to
    # processed/ included.
    if outcome == "placed":
        # A copy is an external job: it has no output path for
        # `libraries.note_job_finished`, so its Library is told here, before
        # the job is announced finished, and drops its kept walk (#353).
        _tell_library(inbox, output)
        await _finish(inbox, state, path, flight, output[0])
        await job_manager.finish_external_job(job.id, success=True)
        return
    if outcome == "cancelled":
        await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                    "Its intake was cancelled", {"platform": flight.platform})
        await job_manager.finish_external_job_cancelled(job.id, message="Cancelled")
        return
    if outcome in ("changed", "deferred"):
        # Not a failure of the file: it is left to settle and be taken again.
        state.seen.pop(path, None)
        if outcome == "deferred":
            why = why or "the storage is still being read by an earlier check"
        await job_manager.finish_external_job(
            job.id, success=False, error_message=f"{why}; it is taken again once it settles",
        )
        return
    await _hold(inbox, state, path, flight.fingerprint, findings.INBOX_FAILED,
                f"Copying it into the Library failed: {why}", {"platform": flight.platform})
    await job_manager.finish_external_job(job.id, success=False, error_message=why)


def _tell_library(inbox: inbox_store.Inbox, placed: list[str]) -> None:
    """Tell the Inbox's Library what a copy placed. On the loop, never waits."""
    from services import libraries

    library = libraries.get(inbox.library_id)
    if library is None:
        return
    try:
        library.note_paths_changed(placed)
    except Exception:
        # Its walk then ages out (SCAN_TTL_SECONDS), as for a conversion.
        logger.warning("inbox: %s could not drop its walk", library.id, exc_info=True)


async def _owned(func, *args: Any, timeout: float, what: str) -> Any:
    """Run a move that can't be taken back (*func*), keeping hold of its
    outcome: its result, or its own failure raised.

    A rename past *timeout* may still land, so it is never taken for one that
    did nothing: intake stops waiting with the bound
    (``run_detached_mutation``) and waits instead for the worker to say it has
    finished, however long that takes, then answers with what it did.
    """
    finished = threading.Event()
    outcome: dict[str, Any] = {}

    def call() -> Any:
        try:
            outcome["result"] = func(*args)
        except BaseException as exc:
            outcome["error"] = exc
            raise
        return outcome["result"]

    try:
        return await run_detached_mutation(call, timeout=timeout, hand_off=lambda: finished.set)
    except MutationInFlight:
        logger.warning("inbox: %s is slow; waiting for it to finish", what)
        while not finished.is_set():
            await asyncio.sleep(_PLACE_POLL_SECONDS)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


async def _place(slot: Any, names: list[str]) -> None:
    """Place a checked copy, keeping hold of the outcome (:func:`_owned`);
    ``slot.placed`` then says whether it landed. Its own failure (a name
    taken) propagates."""
    await _owned(slot.place, names, timeout=_PLACE_SECONDS, what=f"placing {names[0]}")


async def _check_and_place(
    job_id: str, inbox: inbox_store.Inbox, path: str, flight: _Flight, folder: str,
    cancel: asyncio.Event | None, slot: Any, names: list[str],
) -> tuple[str, str, list[str] | None]:
    """Check the staged copy Intact and place it. Under ``_checking()``: the
    stop is read and set under it, so a copy beside one whose check stopped
    neither checks nor places."""
    if _stopped():
        return "deferred", "the storage is still being read by an earlier check", None
    if cancel is not None and cancel.is_set():  # cancelled while it waited
        return "cancelled", "", None
    staged = slot.path_for(names[0])
    # Choosing the checker can open the file (romz lists an archive's
    # members), so it runs on a worker thread, bounded.
    tool = await _step(registry.check_tool, staged, flight.platform)
    if tool is not None:
        await job_manager.update_external_job(job_id, message="Checking the copy is Intact")
        verdict, message, stop = await checks.verify_file(staged, tool.id, cancel)
        # A stop first, whatever the verdict (a cancelled read can leave a
        # reader on the storage too): place nothing, open nothing more.
        if stop:
            _set_stop(True)
            return "deferred", "its check had to stop with the storage still busy", None
        if verdict == checks.CANCELLED:
            return "cancelled", "", None
        if verdict != checks.INTACT:
            return "failed", f"the copy isn't Intact ({message or verdict})", None
    try:
        await _step(_still_taken, inbox, path, flight)
    except _SourceChanged as exc:
        return "changed", str(exc), None
    if cancel is not None and cancel.is_set():
        return "cancelled", "", None
    try:
        await _place(slot, names)
    except FileExistsError:
        return "failed", f"{names[0]} is already in the Library ({folder})", None
    except MutationNotStarted as exc:
        return "failed", f"couldn't place it: {exc}", None
    if not slot.placed:
        return "failed", "placing it in the Library failed", None
    return "placed", "", [os.path.join(folder, name) for name in names]


class _SlotHandoff:
    """Hands a staged copy's slot from its worker to the awaiter, or closes it.

    The worker (``stage``) and the awaiter (``taken`` / ``abandon``) settle,
    under one lock, who owns the slot: the awaiter once it took the result;
    otherwise whoever comes second closes it. So a slot delivered after its
    awaiter gave up (a cancel after the last progress report) is never left
    open. *work* makes the slot: a copy's staging by default, or an
    archive's unpacking (#432); either returns ``(slot, names)``.
    """

    def __init__(self, work: Any = None) -> None:
        self._work = work
        self._lock = threading.Lock()
        self._result: tuple[Any, list[str]] | None = None
        self._abandoned = False
        self._taken = False

    def stage(self, *args: Any, **kwargs: Any) -> tuple[Any, list[str]]:
        result = (self._work or _stage_copy)(*args, **kwargs)
        with self._lock:
            if not self._abandoned:
                self._result = result
                return result
        result[0].close()  # nobody is waiting for it any more
        return result

    def taken(self) -> None:
        with self._lock:
            self._taken = True
            self._result = None

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            delivered, self._result = self._result, None
        if delivered is not None:
            # Delivered, but the answer never reached the awaiter. Closed on
            # a thread of its own: closing touches the disk.
            threading.Thread(target=delivered[0].close, daemon=True).start()


async def _copy_in(
    job_id: str, inbox: inbox_store.Inbox, path: str, flight: _Flight, folder: str,
    cancel: asyncio.Event | None,
) -> tuple[str, str, list[str] | None]:
    """Stage, compare, check Intact and place: ``(outcome, why, placed paths)``."""
    # The shared output locks on every name it will place, as a conversion
    # takes on its outputs, held through the placement: a conversion (here or
    # in the CLI) writing one of those names meanwhile can't collide with it.
    names = flight.placed_names or [os.path.basename(f) for f in flight.files]
    targets = [os.path.join(folder, name) for name in names]
    try:
        refused = await _lock_all(targets, allow_existing=False)
    except DeadlineExpired:
        return "deferred", "its output locks didn't answer in time", None
    except (asyncio.TimeoutError, OSError, LockFailed) as exc:
        return "failed", f"its output couldn't be locked: {exc}", None
    if refused is not None:
        if await _step(os.path.lexists, refused):
            taken = os.path.basename(refused)
            return "failed", f"{taken} is already in the Library ({folder})", None
        return "deferred", f"another job is writing {os.path.basename(refused)}", None
    try:
        return await _copy_locked(job_id, inbox, path, flight, folder, cancel)
    finally:
        try:
            await _step(_release_locks, targets)
        except (asyncio.TimeoutError, OSError):
            logger.warning("inbox: could not release the output locks of %s", path, exc_info=True)


def _release_locks(paths: list[str]) -> None:
    for target in reversed(paths):
        lock_manager.release_lock(target)


async def _copy_locked(
    job_id: str, inbox: inbox_store.Inbox, path: str, flight: _Flight, folder: str,
    cancel: asyncio.Event | None,
) -> tuple[str, str, list[str] | None]:
    """Stage, compare, check and place, with the output locks held."""
    await job_manager.update_external_job(job_id, message=f"Copying into {folder}")
    handoff = _SlotHandoff()
    try:
        slot, names = await run_detached_within_stall_bound(
            handoff.stage, inbox, flight, folder, what=f"Copying {os.path.basename(path)}",
            cancel_event=cancel, report_progress=True,
        )
    except BaseException as exc:
        # Given up on (a cancel, the stall bound, or the task's own cancel):
        # a slot the worker delivers now, or already did, is closed.
        handoff.abandon()
        if isinstance(exc, ReadCancelled):
            return "cancelled", "", None
        if isinstance(exc, _Deferred):
            return "deferred", str(exc), None
        if isinstance(exc, _SourceChanged):
            return "changed", str(exc), None
        if isinstance(exc, _CopyDiffers):
            return "failed", str(exc), None
        raise
    handoff.taken()
    try:
        async with _checking():
            return await _check_and_place(
                job_id, inbox, path, flight, folder, cancel, slot, names,
            )
    finally:
        try:
            await _step(slot.close)
        except (OSError, asyncio.TimeoutError):
            logger.warning("inbox: could not close the staging slot for %s", path, exc_info=True)
