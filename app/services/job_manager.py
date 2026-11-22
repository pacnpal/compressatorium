import asyncio
import contextlib
import inspect
import itertools
import logging
from logging_setup import get_logger
import os
import random
import resource
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
)

from config import settings
from fastapi.concurrency import run_in_threadpool
from models import ConversionJob, ConversionMode, InputKind, JobStatus
from services.archive import archive_service
from services.chd_metadata_store import chd_metadata_store
from services.chdman import ConversionCancelled, chdman_service
from services.disc_id import DiscIdStorageAbandoned
from services.lock_manager import LockFailed, lock_manager
from services.output_conflicts import output_is_part_of_source
from services.mutation_guard import locked_for_mutation
from services import referrers, staging
from services.disk import create_scratch_dir
from services.recognized import recognize
from services.round_trip import RECOGNIZED, recognized_applies, round_trip, round_trip_applies
from services.subprocess_runner import (
    DeadlineExpired,
    MutationInFlight,
    MutationNotStarted,
    ProbeCapacityExceeded,
    ReadCancelled,
    StorageAbandoned,
    clean_tool_output,
    run_detached,
    run_detached_bounded,
    run_detached_mutation,
    run_detached_undoable,
    run_detached_within_stall_bound,
    NoThreadAvailable,
    start_detached_eventually,
    start_detached_or_own_thread,
)
from services.tools import ModeKind, registry
from services.verification_store import check_verdict, verification_store
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.path_utils import (
    BIND_PREFIX,
    is_safe_directory_tree,
    bind_source,
    is_within_configured_volumes,
    queued_source_identity,
    queued_source_problem,
    source_identity_problem,
    source_companions_are_safe,
    strip_archive_path,
)

logger = get_logger("job_manager")

# Delete-on-verify and readers (#306): a Source another job (in any process)
# is reading when the delete step comes is waited for, polling without
# blocking, up to this bound. Still held then, it is kept and the job still
# completes: the conversion succeeded.
DELETE_READER_WAIT_SECONDS = 60.0
DELETE_READER_POLL_SECONDS = 0.5
SOURCE_KEPT_FOR_READERS = "Source kept: it was being read by another job"

# Why a job was cancelled (``ConversionJob.cancel_reason``, #433): someone asked
# (the UI or API, the default), or the app or ``run-rules`` is stopping. A
# caller that reacts to a cancel (the Inbox holds a file the user cancelled)
# reads this, never who cancelled.
CANCEL_REQUESTED = "requested"
CANCEL_SHUTDOWN = "shutdown"

# A processing job silent this long is logged as possibly stalled, and again
# each time this long passes; it is never stopped for it (#542: was
# CHD_DEBUG_PROGRESS_TIMEOUT).
STALL_WARNING_SECONDS = 300
# At DEBUG, a job's progress is logged at most this often.
PROGRESS_LOG_SECONDS = 30

# The glossary names (CONTEXT.md) for each stored check, used in job messages so
# "verified" never stands in for which check an output passed.
_CHECK_LABELS = {
    "intact": "Intact", "round_tripped": "Round-tripped", RECOGNIZED: "Recognized",
}

# What a retried delete of a kept Source found (``retry_kept_delete``, #329).
KEPT_DELETED = "deleted"  # the Source is gone
KEPT_READER = "reader"  # someone still holds it: try again on a later sweep
KEPT_KEPT = "kept"  # the delete step kept it (a Referrer, a path check): try again
KEPT_STALE = "stale"  # what the delete stood on no longer holds: never retry
# An unlink was still running when the retry stopped waiting: the Source may or
# may not be gone. Not an attempt; the next sweep's own checks settle it.
KEPT_UNKNOWN = "unknown"
# Some planned files are removed, not all (an unlink failed after another
# succeeded): the delete has started, and is finished on the next sweep.
KEPT_STARTED = "started"
# The caller stopped (``run-rules`` on SIGTERM) before anything was decided or
# removed: not an attempt, and nothing to record.
KEPT_STOPPED = "stopped"
# How long a retry waits for the Source's volume to answer its stat checks.
KEPT_DELETE_PROBE_SECONDS = 30.0
KEPT_DELETE_UNANSWERED = "the Source's volume did not answer"


class KeptDeleteRun:
    """A retried delete, prepared under its locks (``JobManager.kept_delete``).

    ``refused`` is the outcome when the delete no longer holds; otherwise
    ``delete()`` removes the Source, once. ``started`` is set when a cancel
    stops the delete after it began: what the caller records as started.
    """

    def __init__(self) -> None:
        self.refused: Optional["KeptDeleteRetry"] = None
        self.started: Optional["KeptDeleteRetry"] = None
        self._delete: Optional[Callable] = None

    def set_delete(self, delete: Callable) -> None:
        self._delete = delete

    async def delete(self) -> "KeptDeleteRetry":
        if self._delete is None:
            raise RuntimeError("this retried delete was refused or already ran")
        delete, self._delete = self._delete, None
        return await delete()


class VerifyUnanswered(RuntimeError):
    """An output's check reached no verdict: its bound ran out, or the storage
    stopped answering. A job fails on it like any failed verify; a retried
    delete (#329) counts it as an attempt."""


@dataclass(frozen=True)
class KeptDeleteRetry:
    """The outcome of one :meth:`JobManager.retry_kept_delete`."""

    status: str
    reason: Optional[str] = None
    deleted: Tuple[str, ...] = ()
    # The files the delete planned to remove, in order, once it started: an
    # `unknown` outcome's entry finishes them on the next sweep.
    planned: Tuple[str, ...] = ()


@dataclass
class _SourceDelete:
    """What the shared delete step did (:meth:`JobManager._delete_source_set`)."""

    kept_reason: Optional[str] = None
    cancelled: bool = False  # a cancel seen at the last look, before any unlink
    referrer_records: List[dict] = field(default_factory=list)
    # What a Referrer that applied wants said (``Applied.note``): the RomM
    # swap that couldn't run after the output was placed (#391).
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _DeleteCheck:
    """Which check an output passed, and whether its Source may go
    (:meth:`JobManager._delete_check`).

    ``fail_reason``: a check that ran said the output may not hold the
    Source's content (a failed Round-trip); the job fails, keeping the Source.
    ``keep_reason``: the output is fine but nothing vouches for it (not
    Recognized, #423); the job completes, keeping the Source, and says why.
    """

    check: str
    fail_reason: Optional[str] = None
    keep_reason: Optional[str] = None
    dat_name: Optional[str] = None


def _check_satisfies(check_passed: Optional[str], required: Optional[str]) -> bool:
    """Whether a recorded check meets a required check level: Round-tripped
    meets ``round_trip``, Recognized meets ``recognized``, and any passed
    check meets ``container_only``."""
    if required == "round_trip":
        return check_passed == "round_tripped"
    if required == RECOGNIZED:
        return check_passed == RECOGNIZED
    return check_passed in _CHECK_LABELS


class HeldLocks:
    """Exclusive locks one owner holds, and who lets go of them (#329).

    The owner releases them (:meth:`release`) unless an irreversible mutation
    it stopped waiting for took them first (:meth:`take`, through
    ``JobManager._hand_off_locks``): then that mutation's worker releases them
    once it has finished, and the owner's :meth:`release` is a no-op. The one
    answer to "who owns these locks now", for a job's output locks and a
    retried delete's alike. Thread-safe.
    """

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self._paths: List[str] = list(paths)
        self._mutex = threading.Lock()

    def add(self, path: str) -> None:
        with self._mutex:
            self._paths.append(path)

    def holds(self, path: str) -> bool:
        """Whether *path*'s lock is still held here (not taken by a hand-off)."""
        with self._mutex:
            return path in self._paths

    def take(self) -> List[str]:
        """Every lock still held here, now no longer the owner's to release."""
        with self._mutex:
            paths, self._paths = self._paths, []
        return paths

    def release(self) -> None:
        """Let go of whatever the owner still holds. Blocking (lock file I/O)."""
        _let_go(self.take())


def unfinished_delete(
    planned: Iterable[str], removed: Iterable[str], *, in_flight: bool = False,
) -> Optional[List[str]]:
    """The one decision about a delete step that stopped (#329): once any
    planned file was removed the delete has started, and every outcome but
    "all removed" leaves the files still to remove (the ``deleting``
    lifecycle). None when it never started or finished.

    *in_flight*: the unlinks were handed off while running (a timeout or a
    cancel, ``run_detached_mutation``), so their outcome is unknown whatever
    *removed* says yet; the first unlink may be the one still running.
    That is a started delete, and every planned file
    not seen removed is left to settle: a finish, or ``clear_gone``, checks
    what actually remains, so one that went through just clears."""
    removed = set(removed)
    if not removed and not in_flight:
        return None
    rest = [path for path in planned if path not in removed]
    return rest or None


def _remove_in_order(paths: Iterable[str], removed: List[str]) -> None:
    """Remove every file of *paths* in order, appending each to *removed* as
    it goes. Blocking: one mutation (``run_detached_mutation``) for a whole
    Source set, so a started delete is never left half done."""
    for path in paths:
        os.remove(path)
        removed.append(path)


def _close_opened(opened: object) -> None:
    """The undo of a keeper open (``JobManager._open_keeper``) delivered after
    its caller stopped waiting: close the descriptor nobody will use."""
    fd = opened[0] if isinstance(opened, tuple) and opened else None
    if isinstance(fd, int):
        with contextlib.suppress(OSError):
            os.close(fd)


def _let_go(paths: Optional[Iterable[str]]) -> None:
    """Release the exclusive locks :meth:`JobManager._take_exclusively` took."""
    for path in paths or ():
        lock_manager.release_lock(path)


def _source_bytes(path: str) -> Optional[int]:
    """Size of a job's whole Source set: a directory tree, or every file the
    shared delete plan would remove (CUE/GDI tracks, registry companions such
    as split parts, the archive holding a member). None when unreadable."""
    real = strip_archive_path(path) if "::" in path else path
    if os.path.isdir(real):
        files = (
            os.path.join(root, name)
            for root, _dirs, names in os.walk(real) for name in names
        )
    else:
        try:
            files = build_delete_plan(path).get("delete_paths", []) or [real]
        except Exception:
            files = [real]
    total, seen = 0, False
    for f in files:
        try:
            total += os.path.getsize(f)
            seen = True
        except OSError:
            pass
    return total if seen else None

# Modes for externally-managed jobs that bypass the conversion queue,
# they are driven by callers via create/update/finish_external_job and
# must NOT count against max_queue_depth backpressure
# or the cancel-all / queued-count surfaces that only apply to the
# chdman/dolphin/z3ds conversion pipeline.
@dataclass(frozen=True)
class ExternalMode:
    """What an externally-managed job mode declares about itself.

    Every finished job, external or not, gets a History (Ledger) row
    (restructure PR 4): History is everything that ran, so the Running list
    can be the queue only.
    """

    # Whether a failed job of this mode is "a job you started" (#293's
    # ``job_failed`` message). A check Rule's run is started by the scheduler.
    started_by_hand: bool = True
    # Whether its file work holds one of the ``max_concurrent`` conversion
    # slots (``JobManager.hold_slot``): an Inbox copy (#431) counts toward the
    # same limit as conversions. It still never enters the queue.
    takes_slot: bool = False


EXTERNAL_JOB_MODES: Dict[ConversionMode, ExternalMode] = {
    ConversionMode.METADATA_SCAN: ExternalMode(),
    ConversionMode.DAT_MATCH: ExternalMode(),
    ConversionMode.CHECK: ExternalMode(started_by_hand=False),
    # A find-duplicates run (#388) reads and converts nothing; a person starts it.
    ConversionMode.FIND_DUPLICATES: ExternalMode(),
    # A duplicate delete (#389): a person confirmed it; each copy's delete goes
    # through the job's own delete step, so it converts nothing to record.
    ConversionMode.DELETE_DUPLICATES: ExternalMode(),
    # A patch applied to a cartridge Image (#428): a person started it, and it
    # writes an output, so it has a Ledger row naming the patch file.
    ConversionMode.PATCH: ExternalMode(),
    # A rename to DAT names (#425): a person confirmed it; it renames, so it
    # converts nothing to record.
    ConversionMode.DAT_RENAME: ExternalMode(),
    # An Inbox's copy of an Image no Rule converts (#431): the scheduler starts
    # it, and it converts nothing, so it has no Ledger row.
    ConversionMode.INBOX_COPY: ExternalMode(
        started_by_hand=False, takes_slot=True,
    ),
    # An Inbox unpacking an archive, and Recognizing an Image before it is
    # named (#432): the scheduler starts them, they read and write as much as
    # a copy, and convert nothing.
    ConversionMode.INBOX_UNPACK: ExternalMode(
        started_by_hand=False, takes_slot=True,
    ),
    ConversionMode.INBOX_RECOGNIZE: ExternalMode(
        started_by_hand=False, takes_slot=True,
    ),
}
_EXTERNAL_JOB_MODES = frozenset(EXTERNAL_JOB_MODES)
# Where an external job's placeholder ``file_path`` lives: no Volume path ever
# starts with it, so no in-use check can match one. History names such a job
# by its ``filename`` instead (``ledger.row_from_job``).
EXTERNAL_JOB_PATH = "/__external_jobs__/"


def started_by_hand(mode) -> bool:
    """Whether a job of *mode* is one a person started (every conversion may be)."""
    declared = EXTERNAL_JOB_MODES.get(mode)
    return declared is None or declared.started_by_hand


def is_external_mode(mode) -> bool:
    """Whether *mode* names an externally-managed job, never a conversion."""
    return mode in _EXTERNAL_JOB_MODES


def _trusted_roots(job) -> list[str] | None:
    """The folders a job's Source (and the files its sheet names) may be read
    from when it isn't in a Volume: an archive member's extraction dir
    (``temp_dir``), a bound Source's folder in the work area (#431), or the
    unpack folder it was created to read from (``_trusted_root``, #432). None
    for a Source in a Volume, which stays confined to the Volumes."""
    if getattr(job, "temp_dir", None):
        return [job.temp_dir]
    roots = []
    bound = getattr(job, "_bound", None)
    if bound is not None:
        roots.append(bound.path)
    trusted = getattr(job, "_trusted_root", None)
    if trusted:
        roots.append(trusted)
    return roots or None


def _reads(job) -> str:
    """The path a job reads its Source from: the bound link once its Source
    was bound (#431, ``_bind_source_bounded``), else its own path."""
    return getattr(job, "_read_path", None) or job.file_path


def _set_status_message(job, message: str) -> None:
    """Set *job*'s status line from *message*, a tool's progress or the app's
    own words, cleaned as a job's error is (``clean_tool_output``, #466): no
    backspaces or escape sequences, no blank lines, runs of blanks folded. The
    one place a status message from a tool's progress is set, so every tool
    gets it."""
    job.message = clean_tool_output(message)


def _is_conversion_job(job) -> bool:
    """A real conversion job, not external bookkeeping (metadata scan / DAT match).

    The single definition of "counts toward the conversion queue", used by every
    backpressure / queue-depth surface so they can't drift. It
    is also what takes one of the ``max_concurrent`` slots: the dispatcher admits
    only these, and the concurrency invariant counts only these (#377). An
    external job (a check run, a scan, a DAT match) is PROCESSING from
    ``create_external_job`` on, and never holds a slot.
    """
    return job.mode not in _EXTERNAL_JOB_MODES


def _is_active_conversion(job) -> bool:
    """A conversion job currently occupying the queue (queued or processing)."""
    return (
        job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
        and _is_conversion_job(job)
    )


def _lexical_path(path: str) -> str:
    """*path* as a comparable key without touching the filesystem.

    The answer when the volume will not say: absolute and normalised, so two
    spellings of the same path still collide, but symlink aliases do not.
    """
    return os.path.normpath(os.path.abspath(path))


def _canonical_path(path: str, resolved: Optional[Mapping[str, str]] = None) -> str:
    """*path* as one comparable key: symlinks resolved, or the path as given.

    Split out from :func:`_paths_collide` so a caller comparing one path
    against many resolves each of them once instead of per comparison --
    `realpath` is a blocking stat chain.

    Pass *resolved* (from :func:`_resolve_paths_bounded`) and this never touches
    the filesystem at all: it reads the pre-computed key, falling back to the
    lexical one for a path the pre-flight did not see. That is the form the
    reservation uses, because its work happens on the event loop under
    ``_create_lock`` -- one `realpath` into a dead NFS/SMB mount there is
    uninterruptible, and it freezes every unrelated request in the process
    along with all job creation.
    """
    if resolved is not None:
        return resolved.get(path) or _lexical_path(path)
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _paths_collide(
    path_a: str, path_b: str, resolved: Optional[Mapping[str, str]] = None,
) -> bool:
    return _canonical_path(path_a, resolved) == _canonical_path(path_b, resolved)


# How long the whole pre-flight canonicalisation may take before the batch
# proceeds on lexical keys. Generous, because it covers every path in one
# submit and a healthy mount answers in microseconds; the point is only that a
# mount which never answers cannot hold up job creation forever.
_CANONICAL_PROBE_SECONDS = 20.0
# How long the job waits for a checked output's slot to be set aside
# (``StagedOutput.set_aside``) before it reports the stage folder instead;
# a later answer still moves the records (``_set_aside_output``, #391).
SET_ASIDE_SECONDS = _CANONICAL_PROBE_SECONDS


class _Reservation(NamedTuple):
    """What the locked destination check needs, all of it computed off-loop.

    ``resolved`` maps each path to its canonical key; ``keys`` maps each
    destination to every key a job writing it claims -- itself plus the
    sidecars its mode writes beside it. Both are frozen before the lock is
    taken, so the check does no filesystem work at all: `companion_outputs` is
    *not* pure path math for every tool (makeps3iso probes the disk for its
    numbered split parts), so enumerating it inline would block the event loop
    on exactly the mount the bound exists to survive.
    """

    resolved: Dict[str, str]
    keys: Dict[str, Tuple[str, ...]]
    # ``(source, destination)`` pairs whose destination is the Source or one
    # of its files (``output_conflicts.output_is_part_of_source``), read off
    # the loop with the rest, so the locked check refuses them with no I/O.
    onto_source: frozenset = frozenset()


async def _resolve_paths_bounded(paths: Iterable[str]) -> Dict[str, str]:
    """``{path: canonical key}`` for *paths*, resolved off the event loop.

    Every key is present before the resolution starts, seeded lexically, and
    upgraded in place as each `realpath` returns -- so a bound that expires
    part-way keeps the paths that did answer instead of discarding the lot.

    Detached rather than pooled for the usual reason: a `realpath` on an
    unresponsive mount cannot be cancelled, only abandoned, and abandoning a
    shared pool worker per submit would eventually starve every unrelated
    offload. The thread here is disposable.
    """
    pending = list(dict.fromkeys(paths))
    resolved: Dict[str, str] = {path: _lexical_path(path) for path in pending}
    if not pending:
        return resolved

    # The worker fills its own dict, and the caller only ever sees it once the
    # worker is done. A timed-out `run_detached` abandons the *awaiter*, not
    # the thread: it keeps running, and if it were writing into the map the
    # reservation is using, one destination could compare lexically against an
    # earlier spec and canonically against a later one -- so a symlink alias
    # would slip through a check that is otherwise atomic under the lock.
    produced: Dict[str, str] = {}

    def _resolve_all(progress) -> None:
        for path in pending:
            try:
                produced[path] = os.path.realpath(path)
            except OSError:
                pass  # leave the lexical seed in place
            progress()

    try:
        # Bounded by progress, not by the batch's size: the clock restarts
        # after each path.
        await run_detached_within_stall_bound(
            _resolve_all, what="Canonicalising destinations",
            stall=_CANONICAL_PROBE_SECONDS, report_progress=True,
        )
    except (RuntimeError, asyncio.TimeoutError, OSError):
        logger.warning(
            "Canonicalising %d destination(s) made no progress for %.1fs; "
            "refusing the reservation (a volume is not answering)",
            len(pending),
            _CANONICAL_PROBE_SECONDS,
        )
        # Refused, not degraded. Falling back to the lexical seeds hands two
        # spellings of one file two different keys, so the check that exists to
        # reject the second submission passes both -- see
        # `DestinationUnresolvableError`. Nor is the partial result merged: the
        # map must be frozen before it is used, and a half-applied one compares
        # one destination lexically against an earlier spec and canonically
        # against a later one.
        raise DestinationUnresolvableError(
            "The storage holding the destination stopped responding, so the "
            "output paths could not be checked for collisions. Nothing was "
            "queued; try again once the volume is back."
        ) from None
    resolved.update(produced)
    return resolved


# The statuses a job never leaves. Anything else is still in flight.
_TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
# How often `wait_until_terminal` re-reads job states.
_SETTLE_POLL_SECONDS = 0.05
# The longest a caller inside an HTTP request (the Auto rollback) waits in
# `wait_until_terminal`, whatever the job progress timeout says (0 there means
# no bound). A job still running at this cap is reported as kept, so a short
# cap loses nothing and a stuck job cannot hang the request.
ROLLBACK_WAIT_CAP_SECONDS = 30.0
# The longest a route waits for its per-path Volume checks (and Auto's
# planning) to come back from a disposable thread before it answers 503. A
# Volume that stops answering would otherwise hold the request, or, checked on
# the event loop, every request. The abandoned thread may linger until the
# mount answers; it holds no shared pool worker (`run_detached`).
VOLUME_PROBE_TIMEOUT_SECONDS = 30.0
# How long binding a Source may go without progress (#431): None is the
# conversion's own "no progress for this long" limit.
_BIND_STALL_SECONDS: Optional[float] = None


class DestinationUnresolvableError(RuntimeError):
    """Raised when a destination's canonical identity could not be established.

    The reservation check is only atomic because every destination is reduced
    to one key first. `realpath` is what makes two spellings of one file --
    a symlinked library root and the path underneath it -- compare equal; the
    lexical seed cannot, because it is the spelling.

    Degrading to that seed when the bound expires looked conservative and is
    the opposite. Two submissions naming one file through a link and through
    its real path get *different* lexical keys, so both pass a check whose
    whole purpose is to reject the second. When the mount recovers they write
    the same file -- concurrently once MAX_CONCURRENT_JOBS is above one,
    otherwise one over the other -- and with delete-on-verify both sources are
    removed for the single artifact that survives.

    So the reservation refuses instead. A volume that cannot answer a
    `realpath` inside the bound is a volume the conversion was going to fail
    on anyway; being told so at submit time is strictly better than finding out
    afterwards, with one of the two inputs already gone.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class OutputClaimedError(ValueError):
    """Raised when a destination another live job holds is requested again.

    A ``ValueError`` subclass so the callers that already treat a rejected
    spec as a bad request keep working, but its own type because this is a
    *concurrency* outcome rather than a malformed request: two submissions
    resolved the same destination, and the loser should be told to retry or
    skip (409) rather than shown a 500.
    """

    def __init__(self, detail: str, *, claimed_by: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.claimed_by = claimed_by


class OutputIsSourceError(ValueError):
    """A job's output would be its own Source or one of the Source's files (a
    RAR set's later volume, a split dump's part), whoever queued it (#476).
    Refused before any job of the batch is created: under overwrite the job
    would wait forever on its own input lock, or clear a live part of the
    Source."""


class QueueBackpressureError(RuntimeError):
    """Raised when queue backpressure limits would be exceeded."""

    def __init__(self, current_depth: int, max_depth: int, additional_jobs: int):
        self.current_depth = max(0, int(current_depth))
        self.max_depth = max(0, int(max_depth))
        self.additional_jobs = max(1, int(additional_jobs))
        remaining = max(0, self.max_depth - self.current_depth)
        self.detail = (
            f"Conversion queue is at capacity ({self.current_depth}/{self.max_depth}). "
            f"Retry later or submit <= {remaining} additional job(s)."
        )
        super().__init__(self.detail)


class ExternalJobCancelled(Exception):
    """Raised inside an external-job loop when cancel_job() has been requested."""


class SourceChanged(RuntimeError):
    """A bound Source changed while its job ran: its
    output is discarded, never published, and the Source is left alone."""


class SourceRefused(Exception):
    """A queued conversion's Source isn't the file it was queued for, or
    couldn't be bound (#431): the job ends FAILED with this message, its
    run's own cleanup letting go of everything it holds."""


class JobFeed:
    """Every job's events for one reader (the job stream), each job followed
    from the moment the job manager registers it (#456).

    A reader that only subscribed to the jobs it saw queued or running on
    its own poll missed any job that started and ended between two polls.
    Here the job manager subscribes the feed to each job as it registers
    it, before any event for that job can be sent, so a job's whole life,
    its end state included, lands in the feed however short it is.

    A job may be registered on a worker thread while the reader works on the
    event loop: ``_mutex`` guards the followed jobs, and the per-job queues
    are only ever written on the loop (``_notify_subscribers``).
    """

    def __init__(self, manager: "JobManager") -> None:
        self._manager = manager
        self._queues: dict[str, asyncio.Queue] = {}
        self._closed = False
        self._mutex = threading.Lock()

    def follow(self, job_id: str) -> None:
        """Start following *job_id*; a no-op for one already followed, or
        once the feed is closed (a registration racing ``close``)."""
        with self._mutex:
            if not self._closed and job_id not in self._queues:
                self._queues[job_id] = self._manager.subscribe(job_id)

    def following(self, job_id: str) -> bool:
        with self._mutex:
            return job_id in self._queues

    def followed(self) -> list[tuple[str, asyncio.Queue]]:
        """The jobs followed right now, with each one's queue."""
        with self._mutex:
            return list(self._queues.items())

    def drop(self, job_id: str) -> None:
        """Stop following *job_id* (it ended, or the manager forgot it)."""
        with self._mutex:
            queue = self._queues.pop(job_id, None)
        if queue is not None:
            self._manager.unsubscribe(job_id, queue)

    def close(self) -> None:
        """Stop following every job, and stop being told of new ones."""
        self._manager.unwatch_jobs(self)
        with self._mutex:
            self._closed = True
            queues, self._queues = self._queues, {}
        for job_id, queue in queues.items():
            self._manager.unsubscribe(job_id, queue)


# How often the maintenance loop runs: stale lock cleanup, the stall warning
# and (at DEBUG) the heartbeat line.
MAINTENANCE_INTERVAL_S = 30


class JobManager:
    """Manages conversion job queue and execution."""

    # Constants
    ARCHIVED_JOB_TTL_SECONDS = 60 * 15
    MAX_ARCHIVED_JOBS = 2000

    def __init__(self, max_concurrent: int = 1, max_job_history: int = 500):
        self.jobs: OrderedDict[str, ConversionJob] = OrderedDict()
        self._archived_jobs: OrderedDict[str, Tuple[ConversionJob, float]] = OrderedDict()
        # Called once per job that reaches a terminal status; see
        # `add_terminal_listener` for why anything caring about *how* a job
        # ended has to be told rather than ask later.
        self._terminal_listeners: List[Callable[[ConversionJob], object]] = []
        # Called when a job's recorded outcome changes after it was announced
        # terminal; see `add_amend_listener`.
        self._amend_listeners: List[Callable[[ConversionJob], object]] = []
        self.max_concurrent = max(1, max_concurrent)
        self.max_job_history = max(0, max_job_history)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        # Guards `_subscribers` and `_feeds`: a job can be registered (and a
        # feed subscribed to it) on a worker thread.
        self._subscribers_mutex = threading.Lock()
        # Readers told of every job as it is registered (`watch_jobs`).
        self._feeds: list[JobFeed] = []
        self._cancelled: Set[str] = set()
        self._cancel_events: Dict[str, asyncio.Event] = {}
        # Strong refs to in-flight requeue tasks so the event loop can't
        # garbage-collect them mid-`asyncio.sleep` (see _schedule_dir_lock_requeue).
        self._requeue_tasks: Set[asyncio.Task] = set()
        # Same strong-ref guard for fire-and-forget notify/prune tasks: asyncio
        # only keeps a weak ref to a bare create_task(), so it can be GC'd before
        # it runs (a cancel notification or history prune silently dropped).
        self._background_tasks: Set[asyncio.Task] = set()
        # Runs started as their own task (max_concurrent > 1), so `drain` can
        # wait for each one to finish, its cleanup included.
        self._job_tasks: Set[asyncio.Task] = set()
        self._delete_plans: Dict[str, Dict[str, object]] = {}
        # job id -> the canonical key its output was reserved under. Recorded
        # at queue time because the reservation's pre-flight map is built
        # *before* `_create_lock` is taken (see `_reservation_keys`): two
        # submissions can both pre-resolve, and the second's map cannot contain
        # a job the first queues in between. Without the key kept here the
        # second would fall back to the first job's lexical path, and two
        # spellings of one file -- a symlinked directory and its target --
        # would each be accepted. Dropped when the job leaves the queue; only
        # live jobs are ever consulted.
        self._output_keys: Dict[str, Tuple[str, ...]] = {}
        # The files each running job holds a shared input lock on (#306).
        self._input_locks: Dict[str, List[str]] = {}
        # The live input-lock attempt per job, and the lock guarding it with
        # ``_input_locks``. The acquisition runs on a worker thread that can
        # outlive a cancelled job: it records each lock only while its attempt
        # is still the live one, and releases it otherwise.
        self._input_attempts: Dict[str, object] = {}
        self._input_mutex = threading.Lock()
        # The delete paths a job holds exclusively while it unlinks them.
        self._delete_holds: Dict[str, List[str]] = {}
        # Staging slots holding a checked output that could be neither placed
        # nor set aside: kept open, their locks held, until the process ends,
        # so no sweep removes the only copy (#391).
        self._held_slots: list[staging.StagedOutput] = []
        # Jobs whose end has been announced to terminal listeners, so each is
        # announced exactly once (``_notify_terminal``).
        self._terminal_announced: Set[str] = set()
        self._terminal_mutex = threading.Lock()
        # The companion outputs (extractcd's .bin beside its .cue) each running
        # job holds the same cross-process exclusive lock on as its output.
        # Each running job's output locks (the output and its companions),
        # released by `_release_output_locks` unless an unlink took them.
        self._output_locks: Dict[str, HeldLocks] = {}
        # Jobs currently inside the verify phase, mapped to the monotonic clock
        # reading when that phase began. Verification emits no progress and can
        # legitimately run for many minutes, so the stalled-job warning reports
        # it against its own elapsed time and in its own words rather than
        # calling healthy work stalled (or, as before issue #266, saying nothing
        # at all about a job genuinely wedged in verify).
        self._verifying: dict[str, float] = {}
        self._last_progress_at: Dict[str, float] = {}
        self._last_progress_log_at: Dict[str, float] = {}
        self._last_stall_log_at: Dict[str, float] = {}
        self._pid_stats: Dict[int, Dict[str, int]] = {}
        self._last_output_size: Dict[str, int] = {}
        self._last_output_size_at: Dict[str, float] = {}
        self._running = False
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._debug_task: Optional[asyncio.Task] = None
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        # External jobs holding a conversion slot right now (`hold_slot`).
        self._slot_holders: Set[str] = set()
        # External jobs queued for a slot (`hold_slot`), by id: the dispatcher
        # hands each its local slot when its ticket comes up.
        self._slot_waiters: Dict[str, asyncio.Future] = {}
        self._create_lock = asyncio.Lock()
        # FIFO order of the dispatcher's queue: each queued job (or slot
        # waiter) takes the next number. One process works a data directory
        # at a time (``services.scheduler_lock``), so this needs no disk.
        self._tickets = itertools.count(1)

    def _enforce_queue_backpressure_locked(self, additional_jobs: int = 1) -> None:
        """Raise QueueBackpressureError when queue depth limits are exceeded.

        This method is expected to be called only while holding ``self._create_lock``.
        """
        assert self._create_lock.locked(), (
            "_enforce_queue_backpressure_locked must be called with self._create_lock held"
        )
        max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
        if max_depth <= 0:
            return

        needed = max(1, int(additional_jobs))
        current_depth = sum(
            1 for job in self.jobs.values() if _is_active_conversion(job)
        )
        if current_depth + needed > max_depth:
            raise QueueBackpressureError(
                current_depth=current_depth,
                max_depth=max_depth,
                additional_jobs=needed,
            )

    def _queue_job_locked(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        allow_overwrite: bool = False,
        filename_override: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        container_only: bool = False,
        verify_after: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
        reservation: Optional[_Reservation] = None,
        owner: Optional[str] = None,
        source_identity: Optional[Dict[str, object]] = None,
        trusted_root: Optional[str] = None,
        derived_output_path: Optional[str] = None,
        derive_error: Optional[BaseException] = None,
    ) -> ConversionJob:
        """Queue a job while holding _create_lock (no backpressure check here)."""
        job_id = str(uuid.uuid4())[:8]
        filename = filename_override or os.path.basename(file_path)

        output_path = self._resolve_output_locked(
            file_path, mode, output_dir=output_dir, output_path=output_path,
            resolved=reservation.resolved if reservation else None,
            derived=derived_output_path, derive_error=derive_error,
        )


        # Carry the mode's input kind end-to-end so the pipeline skips the
        # archive-extract / file-only assumptions for a directory job and the
        # lock manager can protect the whole source subtree. Derived from the
        # registry spec (every conversion mode is registered; external jobs
        # bypass this path), so FILE/DIRECTORY can't drift via a typo.
        try:
            input_kind = registry.mode_input_kind(mode.value)
        except KeyError:
            input_kind = InputKind.FILE
        # What the job runs with: its choice, with the tool's default where the
        # mode needs a codec and none was chosen (#345). Every conversion job is
        # built here, whoever queued it (Auto, a single job, the CLI, a Rule),
        # so this is the one place the default applies, and the job records
        # the codec it really ran with (the Ledger, the re-run fast path).
        try:
            compression = registry.for_mode(mode.value).compression_for(
                mode.value, compression,
            )
        except KeyError:
            pass

        job = ConversionJob(
            id=job_id,
            file_path=file_path,
            filename=filename,
            mode=mode,
            status=JobStatus.QUEUED,
            progress=0,
            created_at=datetime.now(timezone.utc),
            output_path=output_path,
            allow_overwrite=allow_overwrite,
            compression=compression,
            delete_on_verify=delete_on_verify,
            container_only=container_only,
            verify_after=verify_after,
            split=split,
            input_kind=input_kind,
        )
        job._sweep_owner = owner
        job._source_identity = source_identity
        job._trusted_root = trusted_root

        self._register_job(job)
        # Every key this job claims -- its destination and the sidecars the
        # mode writes beside it -- for the reservation that runs after this
        # one. See `_output_keys`.
        self._output_keys[job_id] = self._reserved_keys(output_path, reservation)
        if delete_on_verify and delete_snapshot:
            self._delete_plans[job_id] = delete_snapshot
        ticket = next(self._tickets)
        self._queue.put_nowait((ticket, job_id))
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        self._last_progress_log_at[job_id] = now
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Queued job %s mode=%s input=%s output=%s overwrite=%s compression=%s",
                job_id,
                mode.value,
                file_path,
                output_path,
                allow_overwrite,
                compression,
            )
        return job

    async def create_job(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        allow_overwrite: bool = False,
        filename_override: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        container_only: bool = False,
        verify_after: bool = False,
        split: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
    ) -> ConversionJob:
        """Create a new conversion job."""
        specs: List[Dict[str, object]] = [{
            "file_path": file_path,
            "output_dir": output_dir,
            "output_path": output_path,
        }]
        # History first: no await may follow a job's registration, or a
        # cancellation there leaves it queued but never returned (see
        # `create_jobs_atomic`).
        await self._prune_jobs()
        # Outside the lock, and off the event loop: see `_reservation_keys`.
        # The Source capture first, so its wait never sits between resolving
        # the destination keys and comparing them under the lock.
        await self._capture_source_identities(specs, mode)
        reservation = await self._reservation_keys(specs, mode)
        async with self._create_lock:
            self._enforce_queue_backpressure_locked(1)
            # Same destination reservation the batch path gets: a single
            # submit racing a sweep is the same collision with one fewer file.
            self._reject_claimed_destinations_locked(specs, mode, reservation)
            job = self._queue_job_locked(
                file_path=file_path,
                mode=mode,
                output_dir=output_dir,
                output_path=output_path,
                allow_overwrite=allow_overwrite,
                filename_override=filename_override,
                compression=compression,
                delete_on_verify=delete_on_verify,
                container_only=container_only,
                verify_after=verify_after,
                split=split,
                delete_snapshot=delete_snapshot,
                reservation=reservation,
                source_identity=specs[0].get("source_identity"),
                derived_output_path=specs[0].get("derived_output_path"),
                derive_error=specs[0].get("derive_error"),
            )
        return job

    async def create_jobs_atomic(
        self,
        job_specs: List[Dict[str, object]],
        mode: ConversionMode,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        split: bool = False,
        verify_after: bool = False,
        container_only: bool = False,
        owner: Optional[str] = None,
        skipped: Optional[List[tuple]] = None,
        admission: Callable[[], contextlib.AbstractContextManager[object]] | None = None,
    ) -> List[ConversionJob]:
        """Create multiple jobs atomically under a single backpressure check.

        A destination another live job (or an earlier spec) already holds
        refuses the whole batch with ``OutputClaimedError``, unless the caller
        passes a ``skipped`` list (#520): then that one spec is left out, its
        ``(file_path, reason)`` appended, and the rest are queued. Either way no
        two live jobs write one file.

        *admission*, when given, is entered under the queue lock around the
        checks and the queueing, with no await inside: it may raise to refuse
        the whole batch, and holds whatever it holds until every job is queued
        (a sweep's ``libraries.owned_as_listed``, PR #518).
        """
        if not job_specs:
            return []

        jobs: List[ConversionJob] = []
        # History first, before anything is registered. The prune awaits
        # (`delete_job` cleans temp dirs); after registration, a cancellation
        # there would end this call with jobs queued that the caller never got
        # back and so could not withdraw. It removes finished jobs only, so the
        # claim check below answers the same either way.
        await self._prune_jobs()
        # Captured into this attempt's own copies: a submission refused below
        # (backpressure, a claimed destination) captures again when retried,
        # rather than reusing an error from storage that has since recovered.
        # First, so the capture's wait never sits between resolving the
        # destination keys and comparing them under the lock.
        job_specs = [dict(spec) for spec in job_specs]
        await self._capture_source_identities(job_specs, mode)
        # Outside the lock, and off the event loop: see `_reservation_keys`.
        reservation = await self._reservation_keys(job_specs, mode)
        async with self._create_lock:
            with admission() if admission is not None else contextlib.nullcontext():
                self._enforce_queue_backpressure_locked(len(job_specs))
                job_specs = self._reject_claimed_destinations_locked(
                    job_specs, mode, reservation, skipped=skipped,
                )
                for spec in job_specs:
                    file_path = str(spec["file_path"])
                    output_dir = spec.get("output_dir")
                    output_path = spec.get("output_path")
                    filename_override = spec.get("filename_override")
                    jobs.append(
                        self._queue_job_locked(
                            file_path=file_path,
                            mode=mode,
                            output_dir=str(output_dir) if output_dir is not None else None,
                            output_path=str(output_path) if output_path is not None else None,
                            allow_overwrite=bool(spec.get("allow_overwrite", False)),
                            filename_override=(
                                str(filename_override)
                                if filename_override is not None
                                else None
                            ),
                            compression=compression,
                            delete_on_verify=delete_on_verify,
                            container_only=container_only,
                            verify_after=verify_after,
                            split=split,
                            delete_snapshot=spec.get("delete_snapshot"),
                            reservation=reservation,
                            owner=owner,
                            source_identity=spec.get("source_identity"),
                            trusted_root=spec.get("trusted_root"),
                            derived_output_path=spec.get("derived_output_path"),
                            derive_error=spec.get("derive_error"),
                        )
                    )
        return jobs

    async def create_batch_jobs(
        self,
        file_paths: List[str],
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        delete_snapshots: Optional[Dict[str, Dict[str, object]]] = None,
        split: bool = False,
        verify_after: bool = False,
        output_paths: Optional[Dict[str, str]] = None,
        allow_overwrite: bool = False,
        container_only: bool = False,
        owner: Optional[str] = None,
        source_identities: Optional[Dict[str, Dict[str, object]]] = None,
        trusted_roots: Optional[Dict[str, str]] = None,
        admission: Callable[[], contextlib.AbstractContextManager[object]] | None = None,
    ) -> List[ConversionJob]:
        """Create multiple conversion jobs.

        ``output_paths`` overrides the derived destination per input, and
        ``allow_overwrite`` authorises writing over an existing one. Both exist
        so a caller that has already resolved duplicates (the RomM sweep's
        rename/overwrite policy) hands the decision down instead of the queue
        re-deriving a different answer.

        ``source_identities`` maps an input to the identity it must still have
        when its job runs (``utils.path_utils.source_identity``, #431): checked
        under the job's Source lock right before the tool runs, and the job is
        refused when the Source was replaced or is reached through a link.

        ``trusted_roots`` maps an input to the one folder outside the Volumes
        it may be read from (an Inbox's unpack folder in the app's work area,
        #432): set on the job when it is created (``_trusted_root``), so its
        identity check takes the Source there, and only there, and its
        companion and post-conversion checks read beneath it
        (``_trusted_roots``). Only with a ``source_identities`` entry.
        """
        job_specs: List[Dict[str, object]] = []
        for fp in file_paths:
            snapshot = delete_snapshots.get(fp) if delete_snapshots else None
            spec: Dict[str, object] = {
                "file_path": fp,
                "output_dir": output_dir,
                "delete_snapshot": snapshot,
                "allow_overwrite": allow_overwrite,
            }
            override = output_paths.get(fp) if output_paths else None
            if override:
                spec["output_path"] = override
            identity = source_identities.get(fp) if source_identities else None
            if identity:
                spec["source_identity"] = identity
                trusted = trusted_roots.get(fp) if trusted_roots else None
                if trusted:
                    spec["trusted_root"] = trusted
            job_specs.append(spec)
        return await self.create_jobs_atomic(
            job_specs,
            mode,
            compression=compression,
            delete_on_verify=delete_on_verify,
            split=split,
            verify_after=verify_after,
            container_only=container_only,
            owner=owner,
            admission=admission,
        )

    @staticmethod
    def _companions(mode: ConversionMode, output_path: str) -> List[str]:
        """The sibling files this mode writes beside *output_path*.

        Pure path math (`companion_outputs` is documented as such), which is
        what lets the reservation claim them: extractcd writes `Game.cue` *and*
        `Game.bin`, and `romz_extract` can restore that same `Game.bin` as its
        primary. Reserving primaries alone accepted both jobs, and the conflict
        probe each caller runs beforehand cannot catch it -- that runs outside
        `_create_lock`, so two submissions both pass it before either queues.
        """
        try:
            tool = registry.for_mode(mode.value)
            return list(tool.companion_outputs(output_path, mode.value))
        except (KeyError, ValueError, OSError):
            return []

    @staticmethod
    def _reserved_keys(
        output_path: str, reservation: Optional[_Reservation] = None,
    ) -> Tuple[str, ...]:
        """Every canonical key a job writing *output_path* claims.

        Read from the pre-flight, never recomputed: enumerating companions can
        touch the disk (see :meth:`_reservation_keys`), and this runs under
        ``_create_lock``. Without a pre-flight entry the answer is the primary
        alone -- the honest one, since the sidecars cannot be known here
        without the I/O this exists to avoid.
        """
        if reservation is not None:
            found = reservation.keys.get(output_path)
            if found is not None:
                return found
            return (_canonical_path(output_path, reservation.resolved),)
        return (_canonical_path(output_path),)

    @staticmethod
    def _derive_output(
        file_path: str, mode: ConversionMode, output_dir: Optional[str],
    ) -> str:
        """Where *mode* would write *file_path*, as pure derivation.

        Every tool's ``output_path()`` is string work over the stem and the
        mode's suffix -- no stat, no listing. That is what lets the reservation
        canonicalise its destinations *before* taking ``_create_lock``: the
        paths are known without touching the filesystem, so only the
        (bounded, off-loop) `realpath` needs the volume to answer.
        """
        return registry.for_mode(mode.value).output_path(
            mode.value, file_path, output_dir,
        )

    async def _capture_source_identities(
        self, job_specs: list[dict[str, object]], mode: ConversionMode,
    ) -> None:
        """Give every spec queued without one its Source's identity as it is
        now (``queued_source_identity``, #449), so the job can tell when it
        runs that it still reads that file, inside the Volumes. Every
        conversion is queued through here, whoever queued it; an Inbox's
        specs already carry theirs. One that can't be captured carries why
        (``{"error": ...}``) and its job is refused when it runs, with that
        reason. A folder Source (a DIRECTORY mode) has no file identity and is
        re-checked by the folder checks when it runs. Off the event loop,
        bounded by progress rather than by the batch's size: the clock restarts
        after each Source, so a large healthy
        batch is never cut off, and when one stops answering, the Sources
        already captured keep their identities and only the rest carry the
        stall as their error."""
        try:
            if registry.mode_input_kind(mode.value) == InputKind.DIRECTORY:
                return
        except KeyError:
            pass
        wanted = [spec for spec in job_specs if not spec.get("source_identity")]
        if not wanted:
            return

        captured: list[dict[str, object]] = []

        def capture(progress) -> None:
            for spec in wanted:
                file_path = str(spec["file_path"])
                try:
                    identity = queued_source_identity(file_path, mode.value)
                except TimeoutError as exc:  # a Volume root didn't answer: no verdict
                    identity = {"error": str(exc), "unanswered": True}
                except (OSError, ValueError) as exc:
                    identity = {"error": str(exc)}
                progress()  # raises once the wait below has given up
                captured.append(identity)

        try:
            await run_detached_within_stall_bound(
                capture, what="Checking the Sources", stall=_CANONICAL_PROBE_SECONDS,
                report_progress=True,
            )
            unanswered: dict[str, object] = {}
        except (RuntimeError, asyncio.TimeoutError, OSError) as exc:
            # Marked: a Source never reached says nothing about the Image, so
            # a Rule's failed-hold skips it (``sweep._never_checked``).
            unanswered = {
                "error": f"no answer ({exc})" if str(exc) else "no answer", "unanswered": True,
            }
        done = list(captured)  # a late append from an abandoned worker can't land in it
        for index, spec in enumerate(wanted):
            spec["source_identity"] = done[index] if index < len(done) else unanswered

    async def _derive_destinations(
        self, job_specs: List[Dict[str, object]], mode: ConversionMode,
    ) -> None:
        """Derive, once, the destination of every spec queued without one,
        off the event loop and bounded, and carry it in the spec
        (``derived_output_path``, or ``derive_error`` when the tool refused).

        A tool's ``output_path`` is not pure string work for every tool: the
        rar tool reads a set's headers to name the file inside. Deriving it on
        the loop, or again under ``_create_lock``, would block both on a mount
        that stops answering. The reservation and the queue then use the one
        answer. A spec that already carries one is left as it is.
        """
        pending = [
            spec for spec in job_specs
            if spec.get("output_path") is None
            and "derived_output_path" not in spec and "derive_error" not in spec
        ]
        if not pending:
            return
        produced: Dict[int, Tuple[Optional[str], Optional[BaseException]]] = {}

        def _derive_all(progress) -> None:
            for index, spec in enumerate(pending):
                output_dir = spec.get("output_dir")
                try:
                    produced[index] = (self._derive_output(
                        str(spec["file_path"]), mode,
                        str(output_dir) if output_dir is not None else None,
                    ), None)
                except (KeyError, ValueError) as exc:
                    produced[index] = (None, exc)
                progress()

        try:
            await run_detached_within_stall_bound(
                _derive_all, what="Naming the outputs",
                stall=_CANONICAL_PROBE_SECONDS, report_progress=True,
            )
        except (RuntimeError, asyncio.TimeoutError, OSError):
            raise DestinationUnresolvableError(
                "The storage holding a source stopped responding, so its output "
                "could not be named. Nothing was queued; try again once the "
                "volume is back."
            ) from None
        for index, spec in enumerate(pending):
            derived, error = dict(produced).get(index, (None, None))
            if error is not None:
                spec["derive_error"] = error
            elif derived is not None:
                spec["derived_output_path"] = derived

    async def _reservation_keys(
        self, job_specs: List[Dict[str, object]], mode: ConversionMode,
    ) -> _Reservation:
        """Pre-resolve every path the destination reservation will compare.

        Runs before ``_create_lock`` is taken, so the critical section itself
        does no filesystem work: see :func:`_canonical_path`. Covers each
        spec's source and derived destination plus every live job's output,
        because the reservation compares the first set against the second --
        and each destination's *companions*, because a mode that writes
        `Game.cue` also writes `Game.bin`, which another mode can claim as its
        primary.

        The companion lookup happens **here**, inside the bounded detached
        work, not under the lock: it is not pure path math for every tool --
        makeps3iso probes the disk for its numbered split parts -- so calling
        it inline would block the event loop on precisely the unresponsive
        mount this bound exists to survive.

        A spec whose destination cannot be derived (unknown mode, unsupported
        extension) is skipped rather than raised on -- the locked pass runs the
        same derivation and produces the caller-facing error there, once.
        """
        await self._derive_destinations(job_specs, mode)
        # (destination, mode) pairs whose sidecars have to be enumerated.
        wanted: List[Tuple[str, ConversionMode]] = []
        # (source, destination) pairs to hold against the Source's own files.
        sources: List[Tuple[str, str]] = []
        paths: List[str] = []
        for spec in job_specs:
            file_path = str(spec["file_path"])
            paths.append(file_path)
            explicit = spec.get("output_path")
            if explicit is not None:
                destination = str(explicit)
            elif spec.get("derived_output_path") is not None:
                destination = str(spec["derived_output_path"])
            else:
                continue  # it could not be derived: the locked pass raises why
            paths.append(destination)
            wanted.append((destination, mode))
            if registry.spec(mode.value).kind != ModeKind.COPY:
                sources.append((file_path, destination))
        for job in self.jobs.values():
            if not job.output_path:
                continue
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            paths.append(job.output_path)
            wanted.append((job.output_path, job.mode))

        companions: Dict[str, List[str]] = {}
        onto_source: set = set()

        def _enumerate(progress) -> None:
            for destination, job_mode in wanted:
                if destination in companions:
                    continue
                companions[destination] = self._companions(job_mode, destination)
                progress()
            # Every creator's final destination against its Source's own files
            # (a RAR set's volumes read from its headers): the one rule plan_job
            # applies too, held here for the Rules and Inboxes that skip it.
            for source, destination in sources:
                if output_is_part_of_source(destination, source):
                    onto_source.add((source, destination))
                progress()

        try:
            # Bounded by progress, not by the batch's size: the clock restarts
            # after each destination.
            await run_detached_within_stall_bound(
                _enumerate, what="Listing companion outputs",
                stall=_CANONICAL_PROBE_SECONDS, report_progress=True,
            )
        except (RuntimeError, asyncio.TimeoutError, OSError):
            logger.warning(
                "Enumerating companion outputs made no progress for %.1fs; "
                "refusing the reservation (a volume is not answering)",
                _CANONICAL_PROBE_SECONDS,
            )
            # Same reasoning as the canonicalisation below: reserving the
            # primaries only leaves the sidecars unclaimed, so a second
            # submission whose *primary* differs but whose companions land on
            # these passes a check meant to stop it. A cue sheet or a numbered
            # split part written by two jobs is the same lost data as the image
            # itself.
            raise DestinationUnresolvableError(
                "The storage holding the destination stopped responding, so "
                "the files each job would write could not be listed. Nothing "
                "was queued; try again once the volume is back."
            ) from None
        # Snapshot, for the reason `_resolve_paths_bounded` explains: the
        # abandoned worker keeps writing into its own dict.
        found = dict(companions)
        refused = frozenset(onto_source)
        for extra in found.values():
            paths.extend(extra)

        resolved = await _resolve_paths_bounded(paths)
        keys = {
            destination: tuple(dict.fromkeys(
                [_canonical_path(destination, resolved)]
                + [_canonical_path(path, resolved) for path in extra]
            ))
            for destination, extra in found.items()
        }
        return _Reservation(resolved=resolved, keys=keys, onto_source=refused)

    def _resolve_output_locked(
        self,
        file_path: str,
        mode: ConversionMode,
        *,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        resolved: Optional[Mapping[str, str]] = None,
        derived: Optional[str] = None,
        derive_error: Optional[BaseException] = None,
    ) -> str:
        """The destination this job will write, with the pre-creation guards.

        *derived* / *derive_error* are what ``_reservation_keys`` derived for
        the spec off the loop (:meth:`_derive_destinations`): used as they are,
        so nothing is derived again here, under ``_create_lock``. A tool's
        ``output_path`` may read the Source (the rar tool reads a set's headers
        to name its output), so deriving here could block the loop with the
        lock held.

        Split out so a batch can be validated in full before a single job is
        created: raising partway through the creation loop left the earlier
        jobs registered and running while the caller was told the whole batch
        had failed -- and the RomM submit path then retired the re-pin rows for
        conversions that were, in fact, under way.
        """
        if output_path is not None:
            return output_path
        if derive_error is not None:
            raise derive_error
        output_path = (
            derived if derived is not None
            else self._derive_output(file_path, mode, output_dir)
        )
        # The HTTP routes validate inputs before passing an explicit
        # output_path; direct service callers reach this fallback, where each
        # tool's output_path() raises for an extension its mode does not take.
        spec = registry.spec(mode.value)
        # Generic same-path guard: a non-copy mode must never write over its
        # own source (chdman copy is an intentional in-place .chd recompress;
        # every other mode changes the extension so output != input).
        if spec.kind != ModeKind.COPY and _paths_collide(
            output_path, file_path, resolved,
        ):
            raise ValueError(
                "Output path matches input; refusing to overwrite source"
            )
        return output_path

    def _reject_claimed_destinations_locked(
        self,
        job_specs: List[Dict[str, object]],
        mode: ConversionMode,
        reservation: Optional[_Reservation] = None,
        *,
        skipped: Optional[List[tuple]] = None,
    ) -> List[Dict[str, object]]:
        """Refuse the batch if any destination is already spoken for.

        With a ``skipped`` list the claimed specs are left out instead, each
        ``(file_path, reason)`` appended, and the specs still free returned.

        No two live jobs may write the same file. Callers resolve duplicates
        before submitting, but that resolution is a prediction made outside
        this lock: a manual submit and an automation sweep can each settle on
        the same destination before either job starts and takes it, and the
        second then overwrites the first's result -- with delete-on-verify,
        removing both sources for one surviving output.

        Every destination is checked before any job is created, so without a
        ``skipped`` list the batch stays all-or-nothing. Intra-batch
        collisions count too: two specs of one submit resolving to the same
        path is the same bug arriving twice at once.
        """
        # Canonicalise once per path and compare keys. The pairwise version of
        # this re-resolved every earlier destination for every new one, and the
        # live-job lookup re-resolved every live job's output per spec --
        # quadratic in the batch size, in blocking `realpath` calls, on the
        # event loop while `_create_lock` is held. A Select-All submit of a few
        # thousand files made that millions of stat chains against exactly the
        # remote mounts this integration exists for.
        #
        # The keys themselves come pre-resolved from `_reservation_keys`, run
        # off the loop before the lock: even one `realpath` here is a stat
        # chain into the same remote mount, and one that never returns takes
        # the whole process with it.
        active = self._active_output_map(reservation)
        planned: Dict[str, str] = {}   # canonical destination -> source
        free: List[Dict[str, object]] = []
        for spec in job_specs:
            file_path = str(spec["file_path"])
            output_dir = spec.get("output_dir")
            explicit = spec.get("output_path")
            destination = self._resolve_output_locked(
                file_path,
                mode,
                output_dir=str(output_dir) if output_dir is not None else None,
                output_path=str(explicit) if explicit is not None else None,
                resolved=reservation.resolved if reservation else None,
                derived=spec.get("derived_output_path"),
                derive_error=spec.get("derive_error"),
            )
            if reservation and (file_path, destination) in reservation.onto_source:
                raise OutputIsSourceError(
                    f"The output {os.path.basename(destination)} would be "
                    f"{os.path.basename(file_path)} or one of its files; nothing "
                    "was queued",
                )
            # Destination *and* sidecars: a mode that writes `Game.cue` also
            # writes `Game.bin`, which another mode can claim as its primary.
            # Every key is checked before any is taken, so a skipped spec
            # claims nothing.
            keys = self._reserved_keys(destination, reservation)
            claim = self._claim_on(keys, active, planned, file_path, destination)
            if claim is not None:
                if skipped is None:
                    raise claim
                skipped.append((file_path, claim.detail))
                continue
            planned.update((key, file_path) for key in keys)
            free.append(spec)
        return free

    @staticmethod
    def _claim_on(
        keys, active: Dict[str, str], planned: Dict[str, str],
        file_path: str, destination: str,
    ) -> Optional[OutputClaimedError]:
        """Why *destination* is taken: by a live job or an earlier spec."""
        for key in keys:
            claimed_by = active.get(key)
            if claimed_by is not None:
                return OutputClaimedError(
                    f"Another queued job ({claimed_by}) is already writing "
                    f"{os.path.basename(destination)}",
                    claimed_by=claimed_by,
                )
            other = planned.get(key)
            if other is not None:
                return OutputClaimedError(
                    "Two files in this batch would write the same output: "
                    f"{os.path.basename(other)} and "
                    f"{os.path.basename(file_path)}",
                )
        return None

    def _active_output_map(
        self, reservation: Optional[_Reservation] = None,
    ) -> Dict[str, str]:
        """``{canonical output path: job id}`` for every live job.

        Built once per batch rather than re-derived per spec: each entry costs
        a `realpath`, which is a blocking stat chain, and this runs under
        ``_create_lock`` on the event loop -- so the keys come from the
        pre-flight in *resolved*. Includes each job's companion outputs:
        `extractcd` writes a `.bin` beside its `.cue`, and another mode can
        name that same `.bin` as its own destination.
        """
        active: Dict[str, str] = {}
        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            if job.output_path:
                # The keys the job was queued under, when it has them: the
                # pre-flight map in *resolved* is older than any job queued
                # since it was built, and falling back to lexical for those
                # would miss an alias of a path already claimed.
                keys = self._output_keys.get(job.id)
                if keys is None:
                    keys = self._reserved_keys(job.output_path, reservation)
                for key in keys:
                    active.setdefault(key, job.id)
        return active

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    async def wait_until_terminal(
        self, job_ids: Iterable[str], timeout: float | None,
    ) -> None:
        """Wait until every job in *job_ids* is terminal, at most *timeout* s.

        ``cancel_job()`` returning True for a processing job means only that
        its cancel event is set; a worker past its last check still finishes
        the job. A caller that must know how a cancelled job really ended
        waits here, then reads ``get_job()``. A job the queue no longer knows
        counts as settled. None means no bound. Returns at the bound with any
        job still running; the caller decides what that means.
        """
        ids = list(job_ids)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            running = [
                job_id for job_id in ids
                if (job := self.jobs.get(job_id)) is not None
                and job.status not in _TERMINAL_STATUSES
            ]
            if not running or (deadline is not None and loop.time() >= deadline):
                return
            await asyncio.sleep(_SETTLE_POLL_SECONDS)

    def add_terminal_listener(self, callback: Callable[[ConversionJob], object]) -> None:
        """Call *callback* once each job reaches a terminal status.

        For consumers that must remember how a job *ended* after the queue has
        forgotten it. History is capped and lives in memory, so asking
        ``get_job()`` later answers "unknown" for anything pruned or predating
        a restart -- and a consumer that then guesses from the filesystem
        cannot tell a finished conversion from a failed one that unlinked the
        old artifact. A listener is told at the moment the answer is still
        known, and can persist it wherever it needs it.

        The callback may be sync or async (a coroutine is scheduled on the
        loop). It runs after the status is final; exceptions are logged and
        swallowed, because a listener must never fail a conversion.

        Registering the same callback twice keeps one registration, so a
        caller that sets itself up on every run (the CLI) is told once per job.
        """
        if callback not in self._terminal_listeners:
            self._terminal_listeners.append(callback)

    def remove_terminal_listener(self, callback: Callable[[ConversionJob], object]) -> None:
        """Stop calling *callback*; a no-op when it is not registered."""
        if callback in self._terminal_listeners:
            self._terminal_listeners.remove(callback)

    def add_amend_listener(self, callback: Callable[[ConversionJob], object]) -> None:
        """Call *callback* when a job's deleted files or Referrer records change
        after it may already have been announced terminal: an apply handed
        off at shutdown that ends later (#391). Sync
        or async, like a terminal listener; for a consumer that persisted
        the job when it ended (the Ledger) to update what it wrote."""
        if callback not in self._amend_listeners:
            self._amend_listeners.append(callback)

    def remove_amend_listener(self, callback: Callable[[ConversionJob], object]) -> None:
        """Stop calling *callback*; a no-op when it is not registered."""
        if callback in self._amend_listeners:
            self._amend_listeners.remove(callback)

    def _notify_amended(self, job: ConversionJob) -> None:
        """Tell every amend listener that *job*'s outcome changed. Never
        raises; on the loop."""
        for callback in list(self._amend_listeners):
            try:
                result = callback(job)
                if inspect.isawaitable(result):
                    self._spawn_background(result)
            except Exception:
                logger.warning("Amend listener failed for %s", job.id, exc_info=True)

    def _notify_terminal(self, job: ConversionJob) -> None:
        """Tell every listener that *job* is finished, once. Never raises.

        Exactly once per job, whichever path gets here first (#306): a queued
        job cancelled while the start path runs reaches both the cancel path
        and the start path's terminal path, and every listener (SSE,
        notifications, the Ledger's counters, check results) must hear it
        once. The flag is set under ``_terminal_mutex`` before notifying.
        """
        if job.status not in _TERMINAL_STATUSES:
            return
        with self._terminal_mutex:
            if job.id in self._terminal_announced:
                return
            self._terminal_announced.add(job.id)
        if not self._terminal_listeners:
            return
        for callback in list(self._terminal_listeners):
            try:
                result = callback(job)
            except Exception:  # a listener must never fail a conversion
                logger.warning(
                    "Terminal-job listener failed for %s", job.id, exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                try:
                    self._spawn_background(result)
                except RuntimeError:
                    # No running loop (a synchronous test harness): the
                    # coroutine is simply not awaited, and the consumer's own
                    # fallback still applies.
                    result.close()

    def _prune_archived_jobs(self) -> None:
        if not self._archived_jobs:
            return

        now = time.monotonic()
        max_keep = max(self.MAX_ARCHIVED_JOBS, self.max_job_history * 2)
        while self._archived_jobs:
            if len(self._archived_jobs) > max_keep:
                self._archived_jobs.popitem(last=False)
                continue
            oldest_job_id, (_, archived_at) = next(iter(self._archived_jobs.items()))
            if now - archived_at <= self.ARCHIVED_JOB_TTL_SECONDS:
                break
            self._archived_jobs.pop(oldest_job_id, None)

    def _archive_job_for_lookup(self, job: ConversionJob) -> None:
        # Without its runtime-only state (descriptors, locks), which can't be
        # deep-copied and means nothing in a snapshot.
        archived = job.archived_copy()
        self._archived_jobs[archived.id] = (archived, time.monotonic())
        self._prune_archived_jobs()

    # ------------------------------------------------------------------
    # External-job API (for tasks that manage their own execution, e.g.
    # metadata scans).  These jobs bypass the conversion queue and must
    # be driven entirely by the caller.
    # ------------------------------------------------------------------

    def create_external_job(
        self,
        filename: str,
        mode: ConversionMode,
        message: str = "",
    ) -> ConversionJob:
        """Create and register an externally-managed job that bypasses the
        conversion queue.  The caller drives state changes via
        :meth:`update_external_job` and :meth:`finish_external_job`."""
        job_id = str(uuid.uuid4())[:8]
        job = ConversionJob(
            id=job_id,
            # Use a sentinel path that will never resolve to a real volume path
            # so that path-in-use checks cannot accidentally match this job.
            file_path=f"{EXTERNAL_JOB_PATH}{job_id}",
            filename=filename,
            mode=mode,
            status=JobStatus.PROCESSING,
            progress=0,
            message=message,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        self._register_job(job)
        # Register a cancel event so external-job loops can check for
        # cancellation (symmetric with dispatcher jobs at _process_job).
        # Event must be created on a running loop; fall back silently in
        # sync test contexts where no loop is active yet.
        try:
            asyncio.get_running_loop()
            self._cancel_events[job_id] = asyncio.Event()
        except RuntimeError:
            pass
        # Enforce max_job_history for external jobs too (best-effort; only runs
        # when there is a running event loop, i.e. production, not sync tests).
        try:
            asyncio.get_running_loop()
            self._spawn_background(self._prune_jobs())
        except RuntimeError:
            pass
        return job

    async def _hand_slot(self, job_id: str, waiter: asyncio.Future) -> None:
        """Give the local slot to the external job *job_id* waiting on
        *waiter*, once one is free. One that stopped waiting meanwhile (a
        cancel) gets none, and a slot taken for it goes back."""
        try:
            if waiter.done():
                return
            await self._semaphore.acquire()
            if waiter.done():
                self._semaphore.release()
                return
            waiter.set_result(True)
        finally:
            self._slot_waiters.pop(job_id, None)

    async def hold_slot(
        self, job_id: str, cancel_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Take one of the ``max_concurrent`` conversion slots for the external
        job *job_id*, whose mode declares ``takes_slot`` (an Inbox copy, #431),
        waiting as long as conversions hold them all. False when
        *cancel_event* fires first (nothing is held then). The caller gives
        it back with :meth:`release_slot` on every outcome.

        With the dispatcher running it joins the conversions' FIFO: it reserves
        its ticket now, as a queued conversion does,
        and the dispatcher hands it the local slot when that ticket comes up
        (:meth:`_hand_slot`). So it never holds the local slot while an older
        conversion waits for it, and with one slot it can't deadlock behind
        one. Without the dispatcher (a process that only runs jobs it was
        given) the local slot is taken directly.
        """
        job = self.jobs.get(job_id)
        declared = EXTERNAL_JOB_MODES.get(job.mode) if job is not None else None
        if declared is None or not declared.takes_slot:
            raise ValueError(f"Job {job_id} does not take a conversion slot")
        semaphore = self._semaphore
        if self._running:
            if not await self._queued_for_slot(job_id, cancel_event):
                return False
        elif cancel_event is None:
            await semaphore.acquire()
        else:
            if cancel_event.is_set():
                return False
            acquire = asyncio.ensure_future(semaphore.acquire())
            cancelled = asyncio.ensure_future(cancel_event.wait())
            try:
                await asyncio.wait({acquire, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                # The caller itself was cancelled: the acquisition goes too,
                # and a slot it already took is given back, never left held.
                self._drop_acquisition(semaphore, acquire)
                raise
            finally:
                cancelled.cancel()
            if not acquire.done() or cancel_event.is_set():
                self._drop_acquisition(semaphore, acquire)
                return False
        self._slot_holders.add(job_id)
        return True

    async def _queued_for_slot(
        self, job_id: str, cancel_event: Optional[asyncio.Event],
    ) -> bool:
        """Queue *job_id* for the local slot in ticket order and wait for the
        dispatcher to hand it over. False on a cancel (no slot is held then,
        and the ticket is dropped)."""
        ticket = next(self._tickets)
        waiter = asyncio.get_running_loop().create_future()
        self._slot_waiters[job_id] = waiter
        self._queue.put_nowait((ticket, job_id))
        waits = {waiter}
        cancelled = (
            asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        )
        if cancelled is not None:
            waits.add(cancelled)
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            self._give_up_slot_wait(job_id, waiter)
            raise
        finally:
            if cancelled is not None:
                cancelled.cancel()
        if not waiter.done() or (cancel_event is not None and cancel_event.is_set()):
            self._give_up_slot_wait(job_id, waiter)
            return False
        return True

    def _give_up_slot_wait(self, job_id: str, waiter: asyncio.Future) -> None:
        """Stop waiting for the local slot: one already handed over goes back,
        a pending hand-over never happens."""
        if waiter.done() and not waiter.cancelled():
            self._semaphore.release()
        else:
            waiter.cancel()

    @staticmethod
    def _drop_acquisition(semaphore: asyncio.Semaphore, acquire: asyncio.Future) -> None:
        """Abandon a pending ``semaphore.acquire()``: cancel it if it is still
        waiting, or give back the slot it already took (now, or once its
        result lands, when it is done but not yet seen)."""
        def release_if_taken(future: asyncio.Future) -> None:
            if not future.cancelled() and future.exception() is None:
                semaphore.release()

        if acquire.done():
            release_if_taken(acquire)
        else:
            acquire.cancel()
            # A cancel can lose to an acquisition that already succeeded.
            acquire.add_done_callback(release_if_taken)

    def release_slot(self, job_id: str) -> None:
        """Give back the slot :meth:`hold_slot` took for *job_id*. Idempotent."""
        if job_id in self._slot_holders:
            self._slot_holders.discard(job_id)
            self._semaphore.release()

    def slots_in_use(self) -> int:
        """How many ``max_concurrent`` slots are held: every PROCESSING
        conversion, and every external job holding one (`hold_slot`). What
        the concurrency invariant counts (#377, #431)."""
        return sum(
            1 for job in self.jobs.values()
            if job.status == JobStatus.PROCESSING
            and (_is_conversion_job(job) or job.id in self._slot_holders)
        )

    def get_cancel_event(self, job_id: str) -> Optional[asyncio.Event]:
        """Return the cancel event for *job_id*, if one is registered."""
        return self._cancel_events.get(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        """True once cancel_job() has been requested for *job_id*."""
        return job_id in self._cancelled

    async def update_external_job(
        self,
        job_id: str,
        *,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        event_extra: dict[str, object] | None = None,
    ) -> None:
        """Update the progress/message of an externally-managed job and
        notify SSE subscribers.

        *event_extra* rides on that ``progress`` event, which the jobs stream
        forwards: a check run's ``verification_changes`` for the Image it just
        settled (#305), which the browser's verification store applies in the
        order they arrive.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if message is not None:
            _set_status_message(job, message)
        await self._notify_subscribers(
            job_id,
            {
                **(event_extra or {}),
                "type": "progress",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def finish_external_job(
        self,
        job_id: str,
        *,
        success: bool,
        error_message: Optional[str] = None,
        event_extra: dict[str, object] | None = None,
    ) -> None:
        """Mark an externally-managed job as complete or failed, notify
        subscribers, and archive it.

        *event_extra* rides on that terminal event: a check run's
        ``verification_paths`` (#305), the files whose check state it changed,
        which an open page re-reads in case it missed their progress events.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
        if success:
            job.progress = 100
        job.completed_at = datetime.now(timezone.utc)
        if error_message is not None:
            # Tool output that reached the error loses its control characters
            # (a progress bar's backspaces), for every external job (#455).
            job.error_message = clean_tool_output(error_message)
        if not success:
            # The status line says why it stopped, not its last progress (#466).
            job.message = job.error_message or "Failed"
        # Clean up any spurious cancel state that may have been set by
        # cancel_all while the external task was still running.
        self._cancel_events.pop(job_id, None)
        self._cancelled.discard(job_id)
        self._notify_terminal(job)
        event_type = "complete" if success else "error"
        await self._notify_subscribers(
            job_id,
            {
                **(event_extra or {}),
                "type": event_type,
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
                "error_message": job.error_message,
            },
        )
        self._archive_job_for_lookup(job)
        # Keep the job in self.jobs with its terminal status so that:
        # - /api/jobs continues to list it until the user clears it
        # - the normal "Clear Done" flow can remove it alongside conversion jobs
        # Enforce max_job_history on completion (exclude_id preserves this job).
        await self._prune_jobs(exclude_id=job_id)

    async def finish_external_job_waiting(self, job_id: str, reason: str) -> None:
        """End an externally-managed job that did nothing yet, only because
        it has to wait (an Inbox archive with no room to unpack, #458).

        Not a failure: the job is COMPLETED, its ``warning`` says what it
        waits for, and ``result["waiting"]`` lets a reader (``run-rules``'s
        line, the job list) report it as waiting instead of done. The work is
        tried again by whoever started it."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.warning = reason
        job.result = {"waiting": True}
        await self.finish_external_job(job_id, success=True)

    async def finish_external_job_cancelled(
        self,
        job_id: str,
        *,
        message: Optional[str] = None,
        event_extra: dict[str, object] | None = None,
    ) -> None:
        """Finalize an externally-managed job that was cancelled mid-run.

        Parallels :meth:`finish_external_job` but sets status to CANCELLED
        and emits a ``cancelled`` SSE event so the UI transitions cleanly.
        *event_extra* rides on that event, as in :meth:`finish_external_job`.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc)
        if message is not None:
            _set_status_message(job, message)
        self._cancel_events.pop(job_id, None)
        self._cancelled.discard(job_id)
        self._notify_terminal(job)
        await self._notify_subscribers(
            job_id,
            {
                **(event_extra or {}),
                "type": "cancelled",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )
        self._archive_job_for_lookup(job)
        await self._prune_jobs(exclude_id=job_id)

    def get_job_for_lookup(self, job_id: str) -> Optional[ConversionJob]:
        """Get a live job, or a recently archived one that was deleted from history."""
        job = self.jobs.get(job_id)
        if job is not None:
            return job
        self._prune_archived_jobs()
        archived = self._archived_jobs.pop(job_id, None)
        if archived is None:
            return None
        # Mark as recently accessed so active clients can briefly recover.
        archived_job, _ = archived
        self._archived_jobs[job_id] = (archived_job, time.monotonic())
        return archived_job

    def get_all_jobs(self) -> List[ConversionJob]:
        """Get all jobs."""
        return list(self.jobs.values())

    def get_queue_depth(self) -> int:
        """Return queued + processing job count for backpressure checks.

        External jobs (e.g. METADATA_SCAN) are excluded so they cannot
        consume queue capacity or trigger false backpressure errors.
        """
        return sum(
            1 for job in self.jobs.values() if _is_active_conversion(job)
        )

    def _active_jobs_snapshot(self) -> List[ConversionJob]:
        """The queued and running jobs, copied. In memory only: safe on the loop.

        A copy, so resolving their paths later (off the loop) reads the jobs as
        they were here, not a job a running conversion is updating.
        """
        return [
            job.model_copy() for job in list(self.jobs.values())
            if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
        ]

    def _candidates_of(self, jobs: List[ConversionJob]) -> List[Tuple[str, List[str]]]:
        """Each job's candidate paths (input/output). Blocking: a sheet Source's
        tracks are read from the sheet and stat'ed (``_track_candidate_paths``)."""
        return [(job.id, self._candidate_paths(job)) for job in jobs]

    def get_active_job_candidates(self) -> List[Tuple[str, List[str]]]:
        """Return active job ids with their candidate paths (input/output).

        Blocking: it reads the disk, so a caller already off the loop (a
        bounded probe, a threadpool step) uses it; code on the loop awaits
        :meth:`active_job_candidates` instead.
        """
        return self._candidates_of(self._active_jobs_snapshot())

    async def active_job_candidates(
        self, *, cancel_event: Optional[asyncio.Event] = None,
        stall: Optional[float] = None,
    ) -> List[Tuple[str, List[str]]]:
        """:meth:`get_active_job_candidates` for code on the event loop (#388).

        The jobs are snapshot here, in memory; their filesystem-dependent paths
        are resolved in one detached step under the shared stall bound, so a
        mount that stops answering costs the caller ``Stalled``, never the
        loop (*stall*: the bound, the shared one by default). ``ReadCancelled``
        on a cancel.
        """
        jobs = self._active_jobs_snapshot()
        return await run_detached_within_stall_bound(
            self._candidates_of, jobs, what="resolving the active jobs' paths",
            cancel_event=cancel_event, stall=stall,
        )

    @staticmethod
    def _normalize_path(path: str) -> Optional[Path]:
        try:
            return Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _is_descendant(target: Path, ancestor: Path) -> bool:
        """Whether ``target`` is at or below ``ancestor`` (pure path prefix)."""
        try:
            target.relative_to(ancestor)
            return True
        except ValueError:
            return False

    def _directory_job_blocks(self, job: ConversionJob, target: Path) -> bool:
        """True when ``target`` lives inside an active directory job's source.

        A directory job (makeps3iso folder->iso) is packaging its whole source
        subtree, so a per-file job / rename / delete on any path *under* that
        folder — e.g. ``<folder>/PS3_GAME/PARAM.SFO`` while ``<folder>`` is being
        packed — would corrupt the in-flight ISO. The bare path-hash lock can't
        see that containment (a child hashes to a different key), so guard it
        here. Cheap: a normalized ``Path`` prefix check, no extra disk I/O.
        """
        if job.input_kind != InputKind.DIRECTORY:
            return False
        job_dir = self._normalize_path(job.file_path)
        if job_dir is None:
            return False
        return self._is_descendant(target, job_dir)

    def _blocked_by_dir_lock(self, job: ConversionJob) -> bool:
        """Whether this job's input/output is inside a directory subtree another
        job has locked (a makeps3iso folder->iso job packing that tree).

        Such a conflict is **transient** — it clears when the folder job
        finishes — so the dispatcher waits and re-queues the job rather than
        failing it, exactly like a job waiting its turn in the queue.
        """
        if job.input_kind == InputKind.DIRECTORY:
            return lock_manager.dir_lock_would_conflict(job.file_path)
        # In this process (`_dir_locks`), or in any other: a held lock file on
        # a folder above, up to the Volume root (#306). The output is probed
        # under every identity its lock takes (`_output_keys`: the
        # non-following destination and a symlink's target).
        if job.output_path and lock_manager.output_folder_locked_above(
            job.output_path,
        ) is not None:
            return True
        source = job.file_path.split("::", 1)[0]
        return bool(source) and (
            lock_manager.is_within_locked_dir(source)
            or lock_manager.folder_locked_above(source) is not None
        )

    def _take_output_locks(self, job: ConversionJob) -> bool:
        """The cross-process exclusive lock on the job's output and on every
        companion output its mode declares, or none of them (#306).

        A companion (extractcd's ``Game.bin``) is written as much as the
        primary, so the web guard's exact-path probe must refuse a rename or
        delete of it too, and no other job may take it to write.
        """
        if not lock_manager.acquire_lock(job.output_path, allow_existing=job.allow_overwrite):
            return False
        held: List[str] = []

        def let_go() -> None:
            for taken in held:
                lock_manager.release_lock(taken)
            lock_manager.release_lock(job.output_path)

        if job.input_kind != InputKind.DIRECTORY:
            # Directory modes' split parts are found on disk later and are
            # already guarded by their own prefix match.
            try:
                for path in registry.for_mode(job.mode.value).companion_outputs(
                    job.output_path, job.mode.value,
                ):
                    if not lock_manager.acquire_lock(path, allow_existing=job.allow_overwrite):
                        let_go()
                        return False
                    held.append(path)
            except LockFailed:
                let_go()
                raise
        self._output_locks[job.id] = HeldLocks([job.output_path, *held])
        return True

    def _release_output_locks(self, job: ConversionJob) -> None:
        """Let go of the job's output locks, unless an unlink still running
        took them with it (``HeldLocks``): its worker lets go of them then."""
        held = self._output_locks.pop(job.id, None)
        if held is not None:
            held.release()

    async def _source_identity_problem_bounded(
        self, job: ConversionJob, cancel_event: asyncio.Event | None = None,
    ) -> Optional[str]:
        """Why the job's Source isn't the one it was queued with
        (``_source_identity``), or None. A file Source queued without one, or
        whose identity couldn't be captured (#449), is a reason too: nothing
        tells what it reads now from what it was queued for. A folder Source
        (DIRECTORY) has none; the folder checks cover it. Bounded: a Source
        that doesn't answer is a reason, never a pass, and is marked
        ``unanswered`` on the job's identity like a stalled capture, so a
        Rule's failed-hold doesn't take it for a verdict. A cancel
        (*cancel_event*) stops the wait and ends the job CANCELLED."""
        if job.input_kind == InputKind.DIRECTORY:
            return None
        identity = job._source_identity
        if not identity:
            return "the job was queued without its Source's identity, so it can't be checked"
        if "error" in identity:
            return f"the Source couldn't be checked when it was queued ({identity['error']})"
        def problem() -> Optional[str]:
            if identity.get("in_place"):
                # The Source first: its sheet isn't read for the companions
                # until it is the one queued, inside the Volumes. Then its companions too:
                # the converter opens a split set's parts and a sheet's tracks
                # itself.
                return queued_source_problem(job.file_path, identity) or queued_source_problem(
                    job.file_path, identity, job.mode.value,
                )
            return source_identity_problem(job.file_path, identity, job._trusted_root)

        try:
            return await asyncio.wait_for(
                run_detached(problem, cancel_event=cancel_event), _CANONICAL_PROBE_SECONDS,
            )
        except ReadCancelled as exc:
            raise ConversionCancelled("Conversion cancelled") from exc
        except (asyncio.TimeoutError, OSError) as exc:
            # No answer is no verdict on the Image: ``sweep._never_checked`` reads this mark.
            job._source_identity = {**identity, "unanswered": True}
            return f"the Source couldn't be checked ({exc or 'no answer'})"

    async def _bind_source_bounded(
        self, job: ConversionJob, cancel_event: asyncio.Event | None = None,
    ) -> Optional[str]:
        """Bind a job's checked Source before anything else reads it (#431):
        every later step (sizing, detection, the companion checks, the
        converter) reads ``job._read_path``, in a folder of the job's own in
        the app's work area (``disk.create_scratch_dir``), where no Inbox
        writer has any right on the parent chain. None when
        bound (or nothing to bind), else why it couldn't be. Bounded by its
        progress; a cancel (*cancel_event*) stops it and ends the job
        CANCELLED; a late answer's folder is removed."""
        identity = job._source_identity
        if not identity or job.input_kind == InputKind.DIRECTORY:
            return None
        if identity.get("in_place"):
            # A Source in a Volume (#449) isn't bound: it is read by the path
            # it was submitted as, which `queued_source_problem` just found
            # still leads to the file checked. Not the resolved path: a sheet
            # submitted through a link names its tracks beside the link, and
            # that is what the job locked.
            return None
        # A copy fallback (no hard links on a NAS) takes as long as the Image:
        # bounded by its progress, not a fixed deadline.
        guard = threading.Lock()
        state: Dict[str, object] = {"abandoned": False, "result": None}

        def bind(progress):
            into = create_scratch_dir(f"{BIND_PREFIX}{job.id}_")
            result = bind_source(job.file_path, identity, into, progress=progress)
            with guard:
                if not state["abandoned"]:
                    state["result"] = result
                    return result
            result.close()  # nobody is waiting for it any more
            return result

        try:
            bound = await run_detached_within_stall_bound(
                bind, what="Binding the Source", stall=_BIND_STALL_SECONDS,
                report_progress=True, cancel_event=cancel_event,
            )
        except BaseException as exc:
            with guard:
                state["abandoned"] = True
                late = state["result"]
            if late is not None:
                # Answered, but the answer never reached here: removed on a
                # thread of its own (it touches the disk).
                start_detached_eventually(late.close)
            if isinstance(exc, ReadCancelled):
                raise ConversionCancelled("Conversion cancelled") from exc
            if isinstance(exc, (OSError, RuntimeError, asyncio.TimeoutError)):
                if isinstance(exc, TimeoutError):  # no answer: no verdict
                    job._source_identity = {**identity, "unanswered": True}
                return f"the Source couldn't be bound for reading ({exc})"
            raise
        job._bound, job._read_path = bound, bound.read_path
        return None

    async def _require_bound_unchanged(self, job: ConversionJob) -> None:
        """Before any output is published: every bound file, read through the
        descriptor kept from bind time, still has the size, mtime and ctime it
        had then. A hard link shares its inode with the
        Inbox's file, so a resumed download writing it in place shows here.
        Raises :class:`SourceChanged` (the job fails, its staged output is
        discarded and the Source left alone), or when it can't be told."""
        bound = job._bound
        if bound is None:
            return
        try:
            changed = await run_detached_bounded(
                bound.changed, timeout=_CANONICAL_PROBE_SECONDS,
            )
        except (OSError, DeadlineExpired, ProbeCapacityExceeded) as exc:
            raise SourceChanged(
                f"the source changed while it was being converted (couldn't tell: {exc})"
            ) from exc
        if changed is not None:
            raise SourceChanged(
                f"the source changed while it was being converted ({changed}); "
                "nothing was published"
            )

    async def _fail_before_start(
        self, job_id: str, job: ConversionJob, message: str, *, dir_lock_held: bool,
    ) -> None:
        """End a job FAILED before its run starts, letting go of everything it
        holds: the input locks taken so far and its folder and output locks
        (#306)."""
        self._release_inputs(job_id)
        if dir_lock_held:
            lock_manager.release_dir_lock(job.file_path)
        self._release_output_locks(job)
        if job.status in _TERMINAL_STATUSES:
            # Already ended another way (a cancel that landed while this path
            # ran): keep that ending, let go of the rest.
            self._cancel_events.pop(job_id, None)
            return
        job.status = JobStatus.FAILED
        job.error_message = message
        job.completed_at = datetime.now(timezone.utc)
        self._notify_terminal(job)
        await self._notify_subscribers(
            job_id, {"type": "error", "job_id": job_id, "error": message},
        )
        self._cancel_events.pop(job_id, None)
        await self._cleanup_temp_dir(job)
        await self._prune_jobs(exclude_id=job_id)

    async def _defer_blocked_job(
        self,
        job_id: str,
        *,
        output_lock_held: bool,
        message: str = "Waiting for an in-progress folder conversion to finish...",
    ) -> None:
        """Release a job blocked by a directory subtree lock and re-queue it.

        The job waits its turn in the queue and is re-dispatched once the folder
        job releases the lock — the same outcome as any queued job, never a
        failure. Releases the held slot (and the output lock when one was taken)
        so the folder job and others can proceed meanwhile.
        """
        job = self.jobs.get(job_id)
        if output_lock_held and job is not None and job.output_path:
            self._release_output_locks(job)
        if job_id in self._cancel_events:
            del self._cancel_events[job_id]
        if job is not None:
            job.message = message
            await self._notify_subscribers(
                job_id,
                {
                    "type": "status",
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": job.progress,
                    "message": job.message,
                },
            )
        self._schedule_dir_lock_requeue(job_id)

    def _schedule_dir_lock_requeue(self, job_id: str, delay: float = 2.0) -> None:
        """Re-dispatch a deferred job after a short delay so it retries once the
        blocking folder job has had a chance to finish."""

        async def _requeue() -> None:
            try:
                # Jitter: two processes that each took a lock and then backed
                # off from the other's must not retry in lockstep (#306).
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
            except asyncio.CancelledError:
                return
            job = self.jobs.get(job_id)
            if job is None or job_id in self._cancelled:
                return
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return
            job.status = JobStatus.QUEUED
            ticket = next(self._tickets)
            self._queue.put_nowait((ticket, job_id))
            self._last_progress_at[job_id] = time.monotonic()

        # Keep a strong reference until the task finishes; a bare create_task()
        # can be collected while still awaiting the sleep.
        task = asyncio.create_task(_requeue())
        self._requeue_tasks.add(task)
        task.add_done_callback(self._requeue_tasks.discard)

    def _spawn_background(self, coro) -> None:
        """Fire-and-forget *coro*, retaining a strong ref until it completes.

        asyncio holds only a weak reference to a task, so a bare
        ``create_task(coro)`` can be garbage-collected before it runs — a cancel
        notification or history prune may then silently never happen. Mirror
        ``_schedule_dir_lock_requeue``: keep the task in ``_background_tasks`` and
        drop it on completion.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _track_candidate_paths(self, file_path: str) -> List[str]:
        """Sibling *input* files an active job also reads, so rename/delete
        can't pull one out from under a running conversion.

        Two sources, because the two kinds of multi-file input are described
        differently: ``.cue``/``.gdi`` track files are parsed out of the file's
        contents (``build_delete_plan``), while a tool whose source is a *set*
        declares its members through the registry (``source_companions`` — a
        split Wii U dump's ``game_part2.wud`` … ``game_part12.wud``). Without
        the second, deleting or renaming part 5 mid-run would fail a conversion
        that may already have cleared an existing output for overwrite.

        Blocking: ``source_companions`` stats a split set's parts and reads a
        RAR set's headers (bounded, and cached per first volume, #476), so its
        callers run it off the event loop. A set whose members can't be told
        now adds none here (``CompanionsUnknown``, not strict): this tracks
        what a running job reads, and the job's own locks hold the rest.
        """
        if "::" in file_path:
            return []
        if not os.path.isfile(file_path):
            return []
        ext = Path(file_path).suffix.lower()
        if ext in {".cue", ".gdi"}:
            try:
                # Already includes source_companions; one call covers both.
                candidates = build_delete_plan(file_path).get("delete_paths", [])
            except Exception:
                return []
        else:
            candidates = registry.source_companions(file_path)
        source_real = os.path.realpath(file_path)
        tracks = []
        for path in candidates:
            if os.path.realpath(path) == source_real:
                continue
            if os.path.exists(path):
                tracks.append(path)
        return tracks

    def _input_paths(self, job: ConversionJob) -> List[str]:
        """The files a file-input job reads: its Source (the archive, for a
        member) and the Source's companions (cue/gdi tracks, split parts)."""
        if "::" in job.file_path:
            return [job.file_path.split("::", 1)[0]]
        identity = job._source_identity
        if not identity or "error" in identity:
            # No usable identity: refused once its Source's lock is held, and
            # its sheet is never parsed for tracks to lock first.
            return [job.file_path]
        if identity.get("in_place"):
            # The companions captured when it was queued, so nothing is read
            # here: this runs inside the unbounded input-lock wait. The bounded
            # check after the locks are held finds a
            # companion that changed since.
            return [job.file_path, *(each["ref"] for each in identity.get("companions", []))]
        return [job.file_path, *self._track_candidate_paths(job.file_path)]

    def _begin_input_attempt(self, job_id: str) -> object:
        """Start a live input-lock attempt for the job (see ``_hold_inputs``)."""
        attempt = object()
        with self._input_mutex:
            self._input_attempts[job_id] = attempt
        return attempt

    def _hold_inputs(self, job_id: str, job: ConversionJob, attempt: object) -> bool:
        """Take a shared lock on every file the job reads, or none of them.

        Runs on a worker thread, which a cancelled job does not wait for. Each
        lock is recorded only while ``attempt`` is still the job's live one;
        once ``_release_inputs`` has ended it (a cancel, the job's cleanup),
        whatever this thread takes is released at once instead (#306).
        """
        for path in dict.fromkeys(self._input_paths(job)):
            acquired = lock_manager.acquire_input_lock(path)
            with self._input_mutex:
                live = self._input_attempts.get(job_id) is attempt
                if acquired and live:
                    self._input_locks.setdefault(job_id, []).append(path)
                    continue
            if acquired:
                lock_manager.release_input_lock(path)  # the job moved on
            self._release_inputs(job_id)
            return False
        return True

    def _release_inputs(self, job_id: str) -> None:
        with self._input_mutex:
            self._input_attempts.pop(job_id, None)
            paths = self._input_locks.pop(job_id, [])
        for path in paths:
            lock_manager.release_input_lock(path)

    def _try_hold_for_delete(self, job_id: str, paths: Iterable[str]) -> bool:
        """Lock every delete path exclusively, or hold none and say so."""
        taken = self._take_exclusively(paths)
        if taken is None:
            return False
        self._delete_holds.setdefault(job_id, []).extend(taken)
        return True

    @staticmethod
    def _take_exclusively(paths: Iterable[str]) -> Optional[List[str]]:
        """Every path's exclusive lock, or none: the paths taken, or None when
        one is held elsewhere. Blocking. On an error, lets go of what it took."""
        taken: List[str] = []
        try:
            for path in paths:
                if not lock_manager.acquire_lock(path, allow_existing=True):
                    _let_go(taken)
                    return None
                taken.append(path)
        except BaseException:
            _let_go(taken)
            raise
        return taken

    async def _hold_delete_paths_waiting(
        self, job_id: str, cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Trade this job's shared read locks for exclusive locks on every path
        of its delete plan, held until the unlinks finish (#306).

        A path another job reads (a shared lock in any process), or one under a
        folder another process packs, can't be held; this polls, without
        blocking, up to ``DELETE_READER_WAIT_SECONDS`` for it to be let go.
        False when it is still held then, or when the job is cancelled
        meanwhile: the caller keeps the Source (and, for a cancel, ends the job
        CANCELLED). Cancellation is checked before every attempt and again
        right after one succeeds, so a cancel that lands while it sleeps never
        becomes a delete when the reader lets go before the next poll.
        """

        def cancelled() -> bool:
            return bool(cancel_event and cancel_event.is_set()) or job_id in self._cancelled

        snapshot = self._delete_plans.get(job_id) or {}
        paths = list(snapshot.get("paths") or [])
        self._release_inputs(job_id)
        deadline = time.monotonic() + DELETE_READER_WAIT_SECONDS
        while True:
            if cancelled():
                self._release_delete_holds(job_id)
                return False
            if await run_in_threadpool(self._try_hold_for_delete, job_id, paths):
                if cancelled():
                    self._release_delete_holds(job_id)
                    return False
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(DELETE_READER_POLL_SECONDS)

    def _release_delete_holds(self, job_id: str) -> None:
        for path in self._delete_holds.pop(job_id, []):
            lock_manager.release_lock(path)

    def _candidate_paths(self, job: ConversionJob) -> List[str]:
        # The files it reads, as its input locks name them: the captured
        # companions for an in-place Source, so a sheet swapped since it was
        # queued isn't read here.
        paths = self._input_paths(job)
        if job.output_path:
            paths.append(job.output_path)
            # Companion outputs a mode writes beside its primary (extractcd's
            # .bin). Directory modes are skipped: makeps3iso's split parts are
            # queue-time-unknown, disk-probed (companion_outputs scans the dir),
            # and already covered by _split_output_blocks' I/O-free prefix match —
            # so this stays a pure, event-loop-safe lookup (this helper is called
            # synchronously from async route code).
            if job.input_kind != InputKind.DIRECTORY:
                paths.extend(
                    registry.for_mode(job.mode.value).companion_outputs(
                        job.output_path, job.mode.value,
                    )
                )
        return paths

    def find_active_job_for_path(
        self, path: str, *, is_dir: bool = False
    ) -> Optional[ConversionJob]:
        """Return the active job using a path (input/output) or None."""
        target = self._normalize_path(path)
        if target is None:
            return None

        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            # A directory job protects its whole subtree against concurrent
            # mutation: a target *inside* the in-flight folder is in use.
            if self._directory_job_blocks(job, target):
                return job
            # An in-flight split build's numbered parts (Game.iso.0/.1/…) count
            # as the job's output even though they aren't in _candidate_paths.
            if self._split_output_blocks(job, target):
                return job
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return job
                if is_dir:
                    try:
                        cand_path.relative_to(target)
                        return job
                    except ValueError:
                        continue
        return None

    def _is_path_in_use_by_other_job(
        self, job_id: str, path: str, jobs: Optional[Iterable[ConversionJob]] = None,
    ) -> bool:
        """Whether an active job other than *job_id* uses *path*. *jobs* is a
        snapshot of ``self.jobs`` for a caller off the event loop."""
        target = self._normalize_path(path)
        if target is None:
            return False

        for job in (self.jobs.values() if jobs is None else jobs):
            if job.id == job_id:
                continue
            if not _is_active_conversion(job):
                continue
            # Reject a delete that targets a path inside another active
            # directory job's source folder (its whole subtree is in use).
            if self._directory_job_blocks(job, target):
                return True
            if self._split_output_blocks(job, target):
                return True
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return True
        return False

    def _split_output_blocks(self, job: ConversionJob, target: Path) -> bool:
        """Whether ``target`` is a numbered split part of an active split job's
        output (``Game.iso.0``/``.1``/…).

        makeps3iso ``-s`` renames the locked base ``.iso`` to ``.iso.0`` and
        writes ``.iso.1``/… mid-run, so those names don't exist when the job is
        queued and can't be listed in the static ``_candidate_paths`` (which
        carries the base ``output_path``). A prefix check marks the whole set
        in-use so a rename/delete can't mutate a part while it's being written.
        """
        if not job.split or not job.output_path:
            return False
        out = self._normalize_path(job.output_path)
        if out is None or target.parent != out.parent:
            return False
        prefix = out.name + "."
        suffix = target.name[len(prefix):]
        return target.name.startswith(prefix) and suffix.isdigit()

    async def _clear_existing_output(self, job: ConversionJob) -> None:
        """Remove a prior output ahead of an **authorized** overwrite.

        Gated on ``allow_overwrite`` so it never deletes an output the user chose
        to skip/rename — including a split set (``Game.iso.0``/…) that appeared
        after planning but before the worker started (the per-path
        ``acquire_lock`` only sees the bare base name).
        """
        if not job.allow_overwrite or not job.output_path:
            return
        # Tool-neutral: the mode's own plugin enumerates everything to sweep
        # (primary + companions, or for makeps3iso the base plus any numbered
        # split parts). Directory-input jobs take this same path — there is no
        # per-tool branch here, so a second folder-input tool gets correct
        # cleanup instead of inheriting makeps3iso's part logic.
        #
        # Companions are cleared even when the primary is already gone: a lone
        # companion (e.g. a stray extractcd .bin whose .cue was deleted) is what
        # made check_output_conflicts authorize the overwrite, so it must not be
        # left to collide with the new output. Validate the whole set first — a
        # non-file occupant (a directory squatting on the primary or a companion
        # name) can't be unlinked, so reject before removing anything rather than
        # deleting the primary and then failing against the stray occupant.
        tool = registry.for_mode(job.mode.value)
        mode, output_path = job.mode.value, job.output_path

        # Enumeration is inside the hop, not before it: a tool's
        # ``overwrite_targets`` may probe the disk (makeps3iso walks .0/.1/…),
        # which would otherwise block the event loop on a slow/network volume.
        await run_in_threadpool(
            lambda: self._sweep_overwrite_targets(
                tool.overwrite_targets(output_path, mode),
            ),
        )
        # Unconditional: an authorized overwrite replaces whatever lives at this
        # path, so a verification record for it is stale even when the sweep
        # removed nothing — the prior output may have been deleted by something
        # else while the job sat in the queue. Letting the record survive would
        # report the freshly built, unverified artifact as verified.
        await verification_store.clear(output_path)

    @staticmethod
    def _sweep_overwrite_targets(targets: list[str]) -> None:
        """Validate then unlink ``targets``.

        Blocking stat/unlink work, kept in one sync helper so the caller runs
        the whole enumerate-check-remove sequence off the event loop in a single
        hop (the check must stay atomic with respect to its own removals).

        Uses ``lexists``/``islink`` rather than ``exists``/``isfile``: both of
        the latter *follow* symlinks and so report False for a **dangling** one,
        which would leave it in place for the converter to write through —
        potentially landing the output outside the validated volume. Unlinking a
        symlink removes the link itself and never its target, so a link sitting
        on an authorized-overwrite path is safe to clear; anything else that
        isn't a regular file (a directory, a device node) can't be unlinked at
        all and is rejected before anything is removed.
        """
        def _removable(path: str) -> bool:
            return os.path.islink(path) or os.path.isfile(path)

        for target in targets:
            if os.path.lexists(target) and not _removable(target):
                raise RuntimeError("Output path exists and is not a file")
        for target in targets:
            if os.path.lexists(target) and _removable(target):
                os.remove(target)

    async def _prune_jobs(self, *, exclude_id: Optional[str] = None):
        """Trim finished jobs down to ``max_job_history``.

        The cap counts **terminal** jobs only, never the queue. It used to gate on
        the total job count, which conflated pending work with history: submitting
        more than ``max_job_history`` files at once (easy now that Select All spans
        every page — a Redump platform set is thousands) put the total permanently
        over the cap, so every sweep deleted every terminal job it could find and
        still couldn't get under it. The Completed and Failed tabs emptied
        continuously mid-batch, and a run finished with no record of which files
        failed. Queued jobs were never at risk — they simply aren't removable — but
        they were being *counted*, which is what evicted the history.

        Pending and processing jobs are therefore ignored here: a deep queue is a
        queue, not a backlog of history to trim. ``exclude_id`` (the job that just
        finished) still counts toward the cap but is never itself deleted.
        """
        if self.max_job_history <= 0:
            return

        # Insertion order is creation order, so this is oldest-first and the
        # slice below evicts the oldest history first.
        terminal_ids = [
            job_id
            for job_id, job in self.jobs.items()
            if job.status
            in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            )
        ]
        excess = len(terminal_ids) - self.max_job_history
        if excess <= 0:
            return

        removable = [job_id for job_id in terminal_ids if job_id != exclude_id]
        for job_id in removable[:excess]:
            await self.delete_job(job_id)

    async def cancel_job(self, job_id: str, *, reason: str = CANCEL_REQUESTED) -> bool:
        """Cancel a job. *reason* is recorded on it (``cancel_reason``) when
        the cancel is taken; a job already being cancelled keeps its first."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        # Set before the cancel, which may tell the terminal listeners at
        # once (a queued job), and taken back if the cancel isn't taken.
        previous = job.cancel_reason
        if previous is None:
            job.cancel_reason = reason
        accepted = await self._cancel_job(job, job_id)
        if not accepted:
            job.cancel_reason = previous
        return accepted

    async def _cancel_job(self, job: ConversionJob, job_id: str) -> bool:

        # Externally-managed jobs (metadata scan, DAT match) are always
        # created in PROCESSING state, they never sit in the dispatcher
        # queue. Signal via the cancel event; the owning task checks
        # job_manager.is_cancelled() inside its loop and finalizes via
        # finish_external_job_cancelled().
        if job.mode in _EXTERNAL_JOB_MODES:
            if job.status != JobStatus.PROCESSING:
                return False
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            # ASCII ellipsis (matches the conversion-job branch below and
            # the frontend's optimistic "Cancelling..." string, avoids a
            # render flicker when the SSE status event lands).
            job.message = "Cancelling..."
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {
                        "type": "status",
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )
            )
            return True

        if job.status == JobStatus.QUEUED:
            self._cancelled.add(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            # A queued job cancelled before it starts never enters
            # `_process_job`, so its listeners have to be told here.
            self._notify_terminal(job)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {"type": "cancelled", "job_id": job_id, "status": job.status.value},
                )
            )
            await self._cleanup_temp_dir(job)
            return True

        if job.status == JobStatus.PROCESSING:
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            job.message = "Cancelling..."
            self._spawn_background(
                self._notify_subscribers(
                    job_id,
                    {
                        "type": "status",
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )
            )
            return True
        return False

    async def cancel_all_jobs(self, *, reason: str = CANCEL_REQUESTED) -> Dict[str, object]:
        """Cancel all queued and processing jobs, each with *reason*.

        Returns:
            Summary payload with queued/processing counts and job IDs that received
            a cancellation request.
        """
        queued_ids = [
            job.id for job in self.jobs.values()
            if job.status == JobStatus.QUEUED
        ]
        processing_ids = [
            job.id for job in self.jobs.values()
            if job.status == JobStatus.PROCESSING
        ]
        requested_ids: list[str] = []

        # Snapshot IDs first to avoid mutating while iterating jobs.
        for job_id in queued_ids + processing_ids:
            if await self.cancel_job(job_id, reason=reason):
                requested_ids.append(job_id)

        return {
            "requested": len(requested_ids),
            "queued": len(queued_ids),
            "processing": len(processing_ids),
            "job_ids": requested_ids,
        }

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job from the list."""
        if job_id in self.jobs:
            job = self.jobs[job_id]
            if job.status == JobStatus.PROCESSING:
                await self.cancel_job(job_id)
            self._archive_job_for_lookup(job)
            self._cancelled.discard(job_id)
            self._delete_plans.pop(job_id, None)
            self._output_keys.pop(job_id, None)
            if job_id not in self._cancel_events:
                await self._cleanup_temp_dir(job)
            del self.jobs[job_id]
            with self._terminal_mutex:
                self._terminal_announced.discard(job_id)
            return True
        return False

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """Subscribe to progress updates for a job."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._subscribers_mutex:
            self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        """Unsubscribe from progress updates."""
        with self._subscribers_mutex:
            queues = self._subscribers.get(job_id)
            if queues is None:
                return
            with contextlib.suppress(ValueError):
                queues.remove(queue)
            if not queues:
                del self._subscribers[job_id]

    def watch_jobs(self) -> JobFeed:
        """A feed of every job registered from now on, each followed from
        its registration (#456); ``JobFeed.close`` ends it. Jobs registered
        before the call are the caller's to follow (``JobFeed.follow``)."""
        feed = JobFeed(self)
        with self._subscribers_mutex:
            self._feeds.append(feed)
        return feed

    def unwatch_jobs(self, feed: JobFeed) -> None:
        """Stop telling *feed* of new jobs; a no-op when it is not watching."""
        with self._subscribers_mutex, contextlib.suppress(ValueError):
            self._feeds.remove(feed)

    def _register_job(self, job: ConversionJob) -> None:
        """Add *job* to the list and subscribe every open feed to it, before
        anything can send an event for it. The one place a job is
        registered, conversion or external, on the loop or a worker thread."""
        self.jobs[job.id] = job
        with self._subscribers_mutex:
            feeds = list(self._feeds)
        for feed in feeds:
            feed.follow(job.id)

    async def _cleanup_temp_dir(self, job: ConversionJob):
        if job._bound is not None:
            bound, job._bound = job._bound, None
            # Cleanup that must happen: started now whatever the probe slots
            # say (a thread of its own when they are all taken, retried when
            # no thread can start), never skipped.
            closing = start_detached_eventually(bound.close)
            if closing is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(closing), _CANONICAL_PROBE_SECONDS)
                except (asyncio.TimeoutError, OSError):
                    logger.warning("Removing the bound Source folder %s is slow", bound.path)
        if job.temp_dir:
            temp_dir = job.temp_dir
            try:
                if not self._is_safe_temp_dir(temp_dir):
                    logger.warning(
                        "Skipping cleanup for unsafe temp dir %s (job %s)",
                        temp_dir,
                        job.id,
                    )
                    return
                if await run_in_threadpool(os.path.isdir, temp_dir):
                    await run_in_threadpool(shutil.rmtree, temp_dir, ignore_errors=True)
            except Exception as cleanup_error:
                logger.warning("Failed to cleanup temp dir %s: %s", temp_dir, cleanup_error)
            finally:
                job.temp_dir = None

    def _is_safe_temp_dir(self, temp_dir: str) -> bool:
        if not temp_dir:
            return False
        try:
            resolved = Path(temp_dir).resolve(strict=False)
        except (OSError, RuntimeError):
            return False

        bases = []
        if settings.temp_dir:
            try:
                bases.append(Path(settings.temp_dir).resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                logger.debug(
                    "Skipping configured temp_dir %s due to resolution error: %s",
                    settings.temp_dir,
                    exc,
                )
        try:
            bases.append(Path(settings.data_dir).resolve(strict=False) / "temp")
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping data_dir temp base %s due to resolution error: %s",
                settings.data_dir,
                exc,
            )
        try:
            bases.append(Path(tempfile.gettempdir()).resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping system temp base due to resolution error: %s",
                exc,
            )

        for base in bases:
            try:
                resolved.relative_to(base)
                return True
            except ValueError:
                continue
        return False

    async def _notify_subscribers(self, job_id: str, data: dict):
        """Notify all subscribers of a job update."""
        with self._subscribers_mutex:
            queues = list(self._subscribers.get(job_id, ()))
        for queue in queues:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def _set_job_message(self, job_id: str, message: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        _set_status_message(job, message)
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        await self._notify_subscribers(
            job_id,
            {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def _emit_extract_updates(
        self, job_id: str, archive_path: str, internal_path: str, task: asyncio.Task
    ):
        start = time.monotonic()
        while not task.done():
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.PROCESSING:
                return
            elapsed = int(time.monotonic() - start)
            name = os.path.basename(internal_path)
            message = f"Extracting {name}... ({elapsed}s)"
            await self._set_job_message(job_id, message)
            await asyncio.sleep(2)

    async def process_queue(self):
        """Background task to process conversion queue."""
        if self._running:
            return
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())

        self._debug_task = asyncio.create_task(self._debug_loop())
        await self._dispatcher_task

    def has_live_conversions(self) -> bool:
        """Whether a conversion is still queued or running. For a caller
        that drains to a fixed point (``run-rules``'s Inbox pass, #432):
        work that finishes may queue more."""
        return any(
            _is_conversion_job(job) and job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
            for job in list(self.jobs.values())
        )

    async def drain(self) -> None:
        """Wait until every queued job has run to its very end.

        A terminal notification fires before a run is over: `_process_job`
        still removes the job's temp dir (an archive's extracted copy) and
        prunes history after it. A caller that leaves the loop once its jobs
        report finished (the CLI, #306) waits here first, or that cleanup is
        cancelled half-way. Covers inline runs (the queue's `task_done` follows
        them), runs started as tasks, and jobs waiting to be re-queued behind
        a folder lock. Needs the dispatcher running.
        """
        while True:
            await self._queue.join()
            pending = [*self._job_tasks, *self._requeue_tasks]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
            # Drop the finished tasks here: their done callbacks need a loop
            # turn, and awaiting finished tasks may not yield (#450).
            self._job_tasks.difference_update(t for t in pending if t.done())
            self._requeue_tasks.difference_update(t for t in pending if t.done())

    async def shutdown(self) -> None:
        """Stop processing and finish the work listeners started.

        For a caller that owns the event loop and leaves it once its jobs are
        done (the CLI, #306): the dispatcher and maintenance loops stop, and the
        listener tasks still running (a Ledger write) are awaited so nothing a
        finished job reported is lost when the loop closes. The queue, its
        semaphore and the create lock are made new, because asyncio binds them
        to the loop that first waits on them, so the manager can be started
        again under another loop. Call it with no job queued or running.
        """
        self._running = False
        for task in (self._dispatcher_task, self._debug_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._dispatcher_task = None
        self._debug_task = None
        while self._background_tasks:
            snapshot = list(self._background_tasks)
            await asyncio.gather(*snapshot, return_exceptions=True)
            # Remove the finished tasks directly: their done callbacks need a
            # loop turn, and awaiting finished tasks may not yield (#450).
            self._background_tasks.difference_update(t for t in snapshot if t.done())
        self._queue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        # A slot held on the old semaphore is not the new one's to give back.
        self._slot_holders.clear()
        self._create_lock = asyncio.Lock()

    async def _handle_background_maintenance(self) -> None:
        """Remove lock files no process holds any more, once per tick.

        The only recovery a queue needs: locks are ``flock``s, which the
        kernel drops when their holder dies, so a lock file left behind
        never blocks a job (``_try_file_lock`` takes it), and this only
        tidies the lock directory.
        """
        try:
            removed = await run_in_threadpool(lock_manager.cleanup_stale_locks_periodic)
            if removed > 0:
                logger.info("Removed %d stale lock file(s)", removed)
        except Exception as e:
            logger.warning("Stale lock cleanup failed: %s", e)

    def _log_stalled_jobs(self) -> None:
        """Warn about a PROCESSING job whose progress has gone quiet.

        Runs at the default log level and *outside* the DEBUG-only heartbeat
        block: without this the sole trace of a job wedged mid-conversion was
        a debug line nobody would find (issue #263).

        Jobs in the verify phase are reported *as verifying*, not as stalled.
        They used to be skipped outright, because verification emits no progress
        and legitimately runs for many minutes on a large image -- but that also
        meant a job genuinely wedged **in** verify logged nothing at all, which
        is exactly the state issue #266 is about. Verify is now bounded and
        cancellable, so the honest report is what phase the job is in and how
        long it has been there; an operator reading the log can tell a long
        checksum from a hang by whether the line keeps repeating past what the
        file's size can justify.

        Deliberately touches no filesystem. Running at the default log level
        means running for every processing job on every heartbeat, and the mount
        this is meant to report on is precisely the one where ``stat`` blocks in
        uninterruptible I/O -- which would freeze the event loop it is diagnosing.
        ``idle_for`` comes from the in-memory progress clock, and the job message
        already carries bytes written and rate for tools using the size-growth
        fallback.
        """
        now = time.monotonic()
        for job in list(self.jobs.values()):
            if job.status != JobStatus.PROCESSING:
                continue
            verifying_since = self._verifying.get(job.id)
            # In verify, the phase's own clock is the meaningful one: the
            # progress clock stopped at the end of the conversion, so it would
            # report the verify as having been idle since before it started.
            idle_for = now - (
                verifying_since if verifying_since is not None
                else self._last_progress_at.get(job.id, now)
            )
            if idle_for < STALL_WARNING_SECONDS:
                continue
            # None, not 0, for "never logged": monotonic() counts from boot, so
            # a 0 sentinel reads as "logged at boot" and suppressed the first
            # warning for the first STALL_WARNING_SECONDS of uptime.
            last_stall = self._last_stall_log_at.get(job.id)
            if last_stall is not None and now - last_stall < STALL_WARNING_SECONDS:
                continue
            self._last_stall_log_at[job.id] = now
            if verifying_since is not None:
                logger.warning(
                    "Verifying job %s has been in the verify phase for %.1fs "
                    "(bounded; cancellable) input=%s output=%s started_at=%s",
                    job.id,
                    idle_for,
                    job.file_path,
                    job.output_path,
                    job.started_at,
                )
                continue
            logger.warning(
                "Stalled job %s idle=%.1fs progress=%s message=%s input=%s output=%s "
                "started_at=%s",
                job.id,
                idle_for,
                job.progress,
                job.message,
                job.file_path,
                job.output_path,
                job.started_at,
            )

    async def _debug_loop(self):
        while self._running:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL_S)
                await self._handle_background_maintenance()

                # Before the DEBUG gate: this warning must reach a default-level
                # log, not just a debug one.
                self._log_stalled_jobs()

                if not logger.isEnabledFor(logging.DEBUG):
                    continue

                jobs = list(self.jobs.values())
                status_counts = {
                    JobStatus.QUEUED: 0,
                    JobStatus.PROCESSING: 0,
                    JobStatus.COMPLETED: 0,
                    JobStatus.FAILED: 0,
                    JobStatus.CANCELLED: 0,
                }
                for job in jobs:
                    status_counts[job.status] = status_counts.get(job.status, 0) + 1

                subscriber_queues = sum(len(qs) for qs in self._subscribers.values())
                temp_dirs = sum(1 for job in jobs if job.temp_dir)

                usage = resource.getrusage(resource.RUSAGE_SELF)
                rss_raw = usage.ru_maxrss
                if sys.platform == "darwin":
                    rss_mb = rss_raw / (1024 * 1024)
                else:
                    rss_mb = rss_raw / 1024
                open_fds = None
                if os.path.exists("/proc/self/fd"):
                    try:
                        open_fds = len(os.listdir("/proc/self/fd"))
                    except OSError:
                        open_fds = None
                loadavg = None
                if hasattr(os, "getloadavg"):
                    try:
                        loadavg = os.getloadavg()
                    except OSError:
                        loadavg = None

                logger.debug(
                    "Heartbeat jobs=%d queued=%d processing=%d completed=%d failed=%d cancelled=%d "
                    "queue_size=%d semaphore=%s cancelled_set=%d subscribers=%d temp_dirs=%d "
                    "locks=%d active_chdman=%d rss_raw=%d rss_mb=%.1f"
                    " open_fds=%s loadavg=%s",
                    len(jobs),
                    status_counts[JobStatus.QUEUED],
                    status_counts[JobStatus.PROCESSING],
                    status_counts[JobStatus.COMPLETED],
                    status_counts[JobStatus.FAILED],
                    status_counts[JobStatus.CANCELLED],
                    self._queue.qsize(),
                    getattr(self._semaphore, "_value", None),
                    len(self._cancelled),
                    subscriber_queues,
                    temp_dirs,
                    lock_manager.stats().get("locks"),
                    len(chdman_service.active_pids()),
                    rss_raw,
                    rss_mb,
                    open_fds,
                    loadavg,
                )

                for job in jobs:
                    if job.status != JobStatus.PROCESSING:
                        continue
                    now = time.monotonic()
                    last_progress = self._last_progress_at.get(job.id, now)
                    idle_for = now - last_progress
                    output_size = None
                    output_idle = None
                    if job.output_path and os.path.exists(job.output_path):
                        try:
                            output_size = os.path.getsize(job.output_path)
                        except OSError:
                            output_size = None
                        if output_size is not None:
                            last_size = self._last_output_size.get(job.id)
                            last_size_at = self._last_output_size_at.get(job.id, now)
                            if last_size is None or output_size != last_size:
                                self._last_output_size[job.id] = output_size
                                self._last_output_size_at[job.id] = now
                            else:
                                output_idle = now - last_size_at
                    logger.debug(
                        "Processing job %s progress=%s idle=%.1fs output_size=%s output_idle=%s",
                        job.id,
                        job.progress,
                        idle_for,
                        output_size,
                        output_idle,
                    )

                for pid in chdman_service.active_pids():
                    proc_io_path = f"/proc/{pid}/io"
                    proc_status_path = f"/proc/{pid}/status"
                    if not os.path.exists(proc_status_path):
                        self._pid_stats.pop(pid, None)
                        continue

                    rss_kb = None
                    threads = None
                    read_bytes = None
                    write_bytes = None

                    try:
                        with open(proc_status_path, "r", encoding="utf-8") as fh:
                            for line in fh:
                                if line.startswith("VmRSS:"):
                                    rss_kb = int(line.split()[1])
                                elif line.startswith("Threads:"):
                                    threads = int(line.split()[1])
                    except OSError:
                        pass

                    if os.path.exists(proc_io_path):
                        try:
                            with open(proc_io_path, "r", encoding="utf-8") as fh:
                                for line in fh:
                                    if line.startswith("read_bytes:"):
                                        read_bytes = int(line.split()[1])
                                    elif line.startswith("write_bytes:"):
                                        write_bytes = int(line.split()[1])
                        except OSError:
                            pass

                    prev = self._pid_stats.get(pid, {})
                    delta_read = None
                    delta_write = None
                    if read_bytes is not None and "read_bytes" in prev:
                        delta_read = read_bytes - prev["read_bytes"]
                    if write_bytes is not None and "write_bytes" in prev:
                        delta_write = write_bytes - prev["write_bytes"]

                    logger.debug(
                        "chdman pid=%s rss_kb=%s threads=%s read_bytes=%s(+%s) write_bytes=%s(+%s)",
                        pid,
                        rss_kb,
                        threads,
                        read_bytes,
                        delta_read,
                        write_bytes,
                        delta_write,
                    )

                    updated = {}
                    if read_bytes is not None:
                        updated["read_bytes"] = read_bytes
                    if write_bytes is not None:
                        updated["write_bytes"] = write_bytes
                    if updated:
                        self._pid_stats[pid] = updated
            except Exception as exc:
                logger.exception("Debug heartbeat error: %s", exc)

    async def _dispatcher_loop(self):
        """Dispatcher loop that starts jobs in FIFO order with concurrency control."""
        while self._running:
            try:
                _, job_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                job = self.jobs.get(job_id)
                if not job:
                    continue
                waiter = self._slot_waiters.get(job_id)
                if waiter is not None:
                    # An external job queued for a slot (an Inbox copy, #431):
                    # its turn has come, so it gets the local slot, in the same
                    # FIFO order as every conversion.
                    await self._hand_slot(job_id, waiter)
                    continue
                if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
                    self._cancelled.discard(job_id)
                    await self._cleanup_temp_dir(job)
                    continue
                # Only a conversion takes a slot; an external job is driven by
                # its caller and never belongs in the queue (#377).
                if not _is_conversion_job(job):
                    logger.error("Job %s (%s) is not a conversion; not dispatching it",
                                 job_id, job.mode)
                    continue
                await self._semaphore.acquire()
                try:
                    if self.max_concurrent == 1:
                        await self._run_job(job_id)
                    else:
                        task = asyncio.create_task(self._run_job(job_id))
                        self._job_tasks.add(task)
                        task.add_done_callback(self._job_tasks.discard)
                except Exception:
                    # _run_job() always releases the semaphore in its finally block.
                    # Only release here if task creation failed before _run_job started.
                    if self.max_concurrent != 1:
                        self._semaphore.release()
                    raise
            except Exception as e:
                logger.exception("Dispatcher error: %s", e)
            finally:
                self._queue.task_done()

    async def _output_size_bounded(
        self, job: ConversionJob, path: str | None = None,
    ) -> Optional[int]:
        """:meth:`_compute_output_size`, off the loop and bounded (#352).

        Through ``run_detached_within_stall_bound``, the conversion's
        no-progress bound: an output Volume that answers nothing raises
        ``RuntimeError`` naming the output, and the job fails instead of
        holding its queue slot on a ``getsize`` that never returns.
        """
        return await run_detached_within_stall_bound(
            self._compute_output_size, job, path,
            what=f"Sizing {os.path.basename(job.output_path)}",
        )

    def _compute_output_size(
        self, job: ConversionJob, path: str | None = None,
    ) -> Optional[int]:
        """Total bytes of a completed job's output (None if absent).

        Single source of truth for every output shape: the primary output plus
        each companion the mode wrote (extractcd's .bin sidecar, a split
        folder_to_iso's numbered .iso.0/.1/… parts), enumerated from the owning
        tool's ``companion_outputs`` hook rather than re-encoded per mode. A
        split build leaves no bare .iso, so the primary getsize simply misses
        and the numbered parts carry the whole total. Synchronous (a directory
        mode's companion lookup scans the output dir): call it through
        :meth:`_output_size_bounded`. *path* is where the output was written
        when not yet at ``job.output_path`` (a staging slot, #398).
        """
        primary = path or job.output_path
        companions = registry.for_mode(job.mode.value).companion_outputs(
            primary, job.mode.value,
        )
        total_size = 0
        for part in [primary, *companions]:
            try:
                total_size += os.path.getsize(part)
            except OSError:
                pass
        return total_size if total_size > 0 else None

    # --- staging the output out of a Library scanner's sight (#398) --------

    @staticmethod
    async def _needs_stage(job: ConversionJob) -> bool:
        """Whether *job* writes into a hidden staging slot: its output lands
        in any Library declaring ``HIDDEN_STAGING`` (RomM), a folder Library
        inside RomM's folder included (#514). When which Library
        holds it can't be told (a timeout, the Library list unreadable), it
        stages: hiding the work is always safe. No Library, or one without
        the capability (a folder Library), writes in place as before."""
        # Imported here: services.libraries imports this module.
        from services import libraries

        if job._source_identity and not job._source_identity.get("in_place"):
            # A bound Source (#431) may change under its job: its output is
            # staged, so one made from a changed Source is never published.
            return True
        try:
            # Any Library holding the output, not only its owner: RomM scans a
            # folder Library inside its folder too (#514).
            return await run_detached_bounded(
                libraries.hidden_staging, job.output_path, timeout=_CANONICAL_PROBE_SECONDS,
            )
        except Exception:
            logger.warning(
                "Couldn't tell which Library holds %s; staging its output",
                job.output_path, exc_info=True,
            )
            return True

    async def _open_stage(self, job: ConversionJob) -> staging.StagedOutput | None:
        """A staging slot beside the job's output, or None when the job
        writes in place (:meth:`_needs_stage`). A slot that can't be made
        fails the job: never fall back to writing where a scanner looks.

        Staging runs before the tool's runner, which would make a missing
        output folder (a conversion into a new subfolder), so the slot's
        opening makes it, inside the Volume, in the same bounded call. A
        slot that arrives after the bound gave up is closed (``undo``); none
        is opened when no detached worker is free."""
        if not await self._needs_stage(job):
            return None
        folder = os.path.dirname(job.output_path)
        try:
            return await run_detached_undoable(
                staging.open_slot, folder, make_folder=True,
                timeout=_CANONICAL_PROBE_SECONDS, undo=lambda slot: slot.close(),
            )
        except (OSError, DeadlineExpired, ProbeCapacityExceeded) as exc:
            raise RuntimeError(
                f"Could not make the hidden staging folder beside the output: {exc}"
            ) from exc

    def _place_names(self, job: ConversionJob, write_path: str) -> list[str]:
        """The names a placing moves: the output and each companion the mode
        wrote (``companion_outputs``, which may probe the slot). Blocking."""
        companions = registry.for_mode(job.mode.value).companion_outputs(
            write_path, job.mode.value,
        )
        return [os.path.basename(p) for p in [write_path, *companions]]

    def _place_staged_blocking(
        self, job: ConversionJob, slot: staging.StagedOutput, write_path: str,
    ) -> None:
        """The placing :meth:`_place_staged` does, blocking, with no lock
        hand-off: for a caller that owns the job's holds and locks and always
        waits for the outcome itself (the RomM swap's ``_mutate``). A no-op
        once placed. Raises ``OSError`` when nothing
        moved; the records' carrying is logged, never raised."""
        if slot.placed:
            return
        staging.carry_records_blocking(slot.place(self._place_names(job, write_path)))

    async def _place_staged(
        self, job: ConversionJob, slot: staging.StagedOutput | None, write_path: str,
    ) -> None:
        """Move a staged output, and its companions, into place beside the
        Source, then carry their store records (a CHD's disc ID) to the final
        paths. A no-op for a job writing in place.

        A rename is a mutation: one still running when the bound runs out
        keeps the job's output locks until it ends (``run_detached_mutation``)
        and the job fails. A name already taken at the destination fails the
        job with nothing moved (``StagedOutput.place``)."""
        if slot is None or slot.placed:
            return
        try:
            placed = await run_detached_mutation(
                lambda: slot.place(self._place_names(job, write_path)),
                timeout=_CANONICAL_PROBE_SECONDS,
                hand_off=lambda: self._hand_off_locks(job.id, self._output_locks.get(job.id)),
            )
        except (MutationInFlight, MutationNotStarted, OSError) as exc:
            raise RuntimeError(f"Could not place the checked output: {exc}") from exc
        for staged, final in placed:
            for store in (verification_store, chd_metadata_store):
                try:
                    await run_detached_bounded(
                        store.move_blocking, staged, final, timeout=_CANONICAL_PROBE_SECONDS,
                    )
                except Exception:
                    logger.warning(
                        "Could not carry the records of %s to %s", staged, final,
                        exc_info=True,
                    )

    async def _staged_placement(
        self, job: ConversionJob, slot: staging.StagedOutput | None, write_path: str,
    ) -> referrers.StagedPlacement | None:
        """The staged output, for a Referrer that places it itself (#391), or
        None when the job places it now, as before.

        Only for a job that will delete its Source, whose one output file is
        staged (no companions) and whose Image's Library turns on a Referrer
        that places the output (``referrers.places_output``: the RomM swap,
        which must rename the ROM's file onto the output's name before the
        output is there). When any of that can't be told, the job places the
        output now, and a Referrer that needed it staged doesn't run."""
        if slot is None or not job.delete_on_verify or "::" in job.file_path:
            return None
        paths = list((self._delete_plans.get(job.id) or {}).get("paths") or [])
        if not paths:
            return None
        try:
            names = await run_detached_bounded(
                self._place_names, job, write_path, timeout=_CANONICAL_PROBE_SECONDS,
            )
            library = await self._image_library(job.file_path, None)
        except Exception:
            logger.warning(
                "Couldn't tell whether a Referrer places %s; placing it now",
                job.output_path, exc_info=True,
            )
            return None
        if len(names) != 1 or not referrers.places_output(
            job.file_path, {path: job.output_path for path in paths}, library,
        ):
            return None
        return referrers.StagedPlacement(
            staged={job.output_path: write_path},
            place=lambda: self._place_staged(job, slot, write_path),
            place_blocking=lambda: self._place_staged_blocking(job, slot, write_path),
            is_placed=lambda: slot.placed,
            source_fingerprints=dict(
                (self._delete_plans.get(job.id) or {}).get("fingerprints") or {},
            ),
        )

    async def _set_aside_output(
        self, job: ConversionJob, slot: staging.StagedOutput,
    ) -> None:
        """Keep a checked output that was never placed: the only copy, never sweepable.

        ``StagedOutput.set_aside`` renames its slot to a ``park-`` slot no
        sweep removes, or marks it kept in place when it can't be renamed;
        its check and CHD records move with it (the store helpers placing
        uses). If neither works, the slot is held open, its lock kept, for as
        long as the process runs (``_held_slots``), so no sweep can take it.
        The job's message, warning, error (the Ledger's ``error`` for a
        failed job), its Ledger record and the log say where. The caller
        never runs ``_close_stage`` on it. Never raises.

        Setting it aside runs under ``SET_ASIDE_SECONDS`` through
        ``run_detached_undoable``. The slot is
        protected throughout: ``set_aside`` holds its lock until the rename
        or the keep marker is in place. One that answers after the bound gets
        its records moved by its own worker (``late``), which logs the real
        path and adds it to the job's message and warning; the job reports
        the stage folder meanwhile."""
        base = os.path.basename(job.output_path)
        names: list[str] = [base]
        before = slot.path

        def late(kept: str) -> None:
            # On its own thread, once nobody waits for the set-aside any more.
            self._keep_records_blocking(job, names, before, kept)
            note = (
                "its slot was set aside after the job ended; "
                f"it is kept at {os.path.join(kept, base)}"
            )
            logger.warning("Job %s: %s", job.id, note)
            job.message = f"{job.message}; {note}" if job.message else note
            job.warning = f"{job.warning}; {note}" if job.warning else note

        # The set-aside starts before anything a cancel can interrupt:
        # its thread starts now, a detached one or,
        # with none free, one of its own, so the slot
        # is renamed or marked kept whatever happens to this await. An answer
        # nobody waits for any more goes to ``late``, once.
        def mark_kept_later() -> None:
            # On its own thread once one can start: the keep marker, then the
            # slot's lock goes. Until then (or if it fails) the slot is held,
            # its lock kept, which no sweep passes while the process lives.
            try:
                slot.mark_kept()
            except (OSError, ValueError):
                logger.exception("Could not mark %s kept; holding its slot", slot.path)
                return
            with contextlib.suppress(ValueError):
                self._held_slots.remove(slot)
            note = "its keep marker is written; its preservation is durable"
            logger.warning("Job %s: %s", job.id, note)
            job.message = f"{job.message}; {note}" if job.message else note
            job.warning = f"{job.warning}; {note}" if job.warning else note

        future: asyncio.Future | None
        durable = True
        try:
            future = start_detached_or_own_thread(slot.set_aside)
        except NoThreadAvailable:
            # The OS thread limit: the slot is held,
            # its lock kept, so no sweep takes it while the process lives, and
            # the keep marker is written off the loop once a thread starts,
            # never on it: its create can block on a dead Volume.
            future = None
            durable = False
            self._held_slots.append(slot)
            start_detached_eventually(mark_kept_later)
        state = {"waiting": True}

        def answered(done: asyncio.Future) -> None:
            if state["waiting"] or done.cancelled() or done.exception() is not None:
                return
            start_detached_eventually(late, done.result())

        def stop_waiting() -> None:
            state["waiting"] = False
            if future is not None and future.done():
                answered(future)

        if future is not None:
            future.add_done_callback(answered)
        kept: str | None
        try:
            try:
                names = await run_detached_bounded(
                    self._place_names, job, slot.path_for(base),
                    timeout=_CANONICAL_PROBE_SECONDS,
                )
            except Exception:
                logger.warning(
                    "Could not list the outputs of %s", job.output_path, exc_info=True,
                )
            if future is None:
                kept = before  # held above, its marker written when a thread starts
            else:
                kept = await asyncio.wait_for(asyncio.shield(future), SET_ASIDE_SECONDS)
        except asyncio.TimeoutError:
            if (
                future is None
                or (future.done() and not future.cancelled() and future.exception() is not None)
            ):
                kept = before  # its own timeout, not the bound's: it failed
                self._held_slots.append(slot)
            else:
                kept = None  # still running, its lock held; ``late`` takes it from here
                stop_waiting()
        except asyncio.CancelledError:
            stop_waiting()
            raise
        except Exception:
            # Fail closed: the slot stays open, its lock held, so no sweep
            # removes it.
            logger.exception("Could not set aside the checked output of %s", job.output_path)
            self._held_slots.append(slot)
            kept = before
        if kept is None:
            stage = os.path.dirname(before)
            where = stage
            note = (
                f"the checked output was not placed; it is kept in {stage} "
                "(its slot is still being set aside; the log names it once done)"
            )
        else:
            try:
                await run_detached_bounded(
                    self._keep_records_blocking, job, names, before, kept,
                    timeout=_CANONICAL_PROBE_SECONDS,
                )
            except Exception:
                logger.warning("Could not carry the records to %s", kept, exc_info=True)
            where = os.path.join(kept, base)
            note = f"the checked output was not placed; it is kept at {where}"
            if not durable:
                note = (
                    f"{note} (its slot is held while the app runs; preservation is "
                    "not yet durable until its keep marker is written)"
                )
        logger.warning("Job %s: %s", job.id, note)
        job.warning = f"{job.warning}; {note}" if job.warning else note
        job.message = f"{job.message}; {note}" if job.message else note
        if job.status == JobStatus.FAILED:
            job.error_message = (
                f"{job.error_message}; {note}" if job.error_message else note
            )
        job.referrers_updated = [
            *job.referrers_updated,
            {"referrer": "staging", "target": where, "status": "kept", "reason": note},
        ]

    def _keep_records_blocking(
        self, job: ConversionJob, names: list[str], before: str, kept: str,
    ) -> None:
        """Carry a set-aside output's check and CHD records from its slot to
        where it is kept (the store helpers placing uses), and write its check
        record there when placing failed before the job wrote one. Blocking;
        logged, never raised."""
        if kept != before:
            staging.carry_records_blocking(
                (os.path.join(before, name), os.path.join(kept, name)) for name in names
            )
        if not job.check_passed:
            return
        where = os.path.join(kept, os.path.basename(job.output_path))
        try:
            if verification_store.get_record_blocking(where) is None:
                verification_store.mark_verified_blocking(
                    where, source_path=job.file_path,
                    produced_meta=self._verifier_identity(job), check=job.check_passed,
                )
        except Exception:
            logger.warning("Could not record the check of %s", where, exc_info=True)

    async def _close_stage(
        self, job: ConversionJob, slot: staging.StagedOutput | None,
    ) -> None:
        """Close the job's slot: discard what was never placed, and its store
        records, then the slot. Never raises.

        Like ``_let_go_in_background``: the worker starts before the first
        await (``start_detached``), so a cancel can't stop it from starting;
        with no detached slot free it gets a thread of its own, so the slot's
        descriptors and lock are never left open until restart. The wait is
        bounded; what doesn't finish goes on in the background."""
        if slot is None:
            return

        def discard() -> None:
            try:
                if not slot.placed:
                    self._clear_staged_records(job, slot)
            finally:
                slot.close()

        worker = start_detached_eventually(discard)
        if worker is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(worker), _CANONICAL_PROBE_SECONDS)
        except Exception:
            logger.warning(
                "Removing the staging slot %s did not finish", slot.path, exc_info=True,
            )

    def _clear_staged_records(self, job: ConversionJob, slot: staging.StagedOutput) -> None:
        """Drop the store records of a staged output that was never placed (a
        failed check marks it). Blocking; never raises."""
        try:
            names = self._place_names(job, slot.path_for(os.path.basename(job.output_path)))
        except Exception:
            logger.debug("Could not list the staged output of %s", job.output_path, exc_info=True)
            return
        for name in names:
            for store in (verification_store, chd_metadata_store):
                try:
                    store.clear_blocking(slot.path_for(name))
                except Exception:
                    logger.debug("No record to clear for %s", name, exc_info=True)

    @staticmethod
    def _norm_fps(
        fingerprints: Optional[Dict[str, Dict[str, object]]],
    ) -> Dict[str, Dict[str, int]]:
        """Normalize a ``{realpath: {size, mtime_ns, inode, device}}`` map to
        plain ints, so a stored (JSON-round-tripped) fingerprint and a freshly
        computed one compare by equality. ``inode``/``device`` are carried
        alongside size+mtime_ns — the same four fields the delete-safety
        snapshot trusts to authorize an irreversible delete, so a reuse is held
        to no weaker a bar (a copy-restore lands on a new inode, a moved mount on
        a new device)."""
        out: Dict[str, Dict[str, int]] = {}
        for path, fp in (fingerprints or {}).items():
            out[path] = {
                "size": int(fp.get("size", -1)),
                "mtime_ns": int(fp.get("mtime_ns", -1)),
                "inode": int(fp.get("inode", 0) or 0),
                "device": int(fp.get("device", 0) or 0),
            }
        return out

    def _source_fingerprint(
        self, source_path: str,
    ) -> Optional[Dict[str, Dict[str, int]]]:
        """Stat fingerprint of the *complete* source set for ``source_path``.

        Uses ``build_delete_snapshot`` so a ``.cue`` / ``.gdi`` descriptor's
        referenced track files are fingerprinted too — a replaced ``.bin`` track
        (with the descriptor untouched) changes the fingerprint, which a
        descriptor-only check would miss. Returns ``{realpath: {size, mtime_ns,
        inode, device}}`` or ``None`` when the set can't be safely enumerated
        (unsafe/missing tracks, a non-file), so those never qualify for reuse.
        """
        try:
            snapshot = build_delete_snapshot(source_path)
        except Exception:
            return None
        return self._norm_fps(snapshot.get("fingerprints")) or None

    @staticmethod
    def _output_fingerprint(output_path: str) -> Optional[Dict[str, int]]:
        """Stat fingerprint (size + mtime_ns + inode + device) of an output
        file, or ``None``. inode/device catch a replaced file that happens to
        preserve size+mtime (e.g. a copy-restore onto a fresh inode)."""
        try:
            st = os.stat(output_path, follow_symlinks=False)
        except OSError:
            return None
        if not os.path.isfile(output_path):
            return None
        return {
            "size": int(st.st_size),
            "mtime_ns": int(
                getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
            ),
            "inode": int(getattr(st, "st_ino", 0) or 0),
            "device": int(getattr(st, "st_dev", 0) or 0),
        }

    def _build_produced_meta(
        self, job: ConversionJob, output_path: str | None = None,
    ) -> Optional[Dict[str, object]]:
        """Snapshot of what produced this verified artifact, for the re-run fast
        path: the mode + output-shaping settings and stat fingerprints of the
        complete source set and the output.

        *output_path* is where the output is now: its staged path while a
        Referrer is still to place it (#391). A rename keeps the fingerprint.

        The source fingerprint is the **pre-conversion** delete snapshot
        (captured when the job was planned, before the converter read the
        source), re-validated to still describe the on-disk source here. If the
        source changed between planning and this point — e.g. mutated while the
        converter was running — the pre-conversion snapshot no longer matches and
        no reusable metadata is recorded, so a later re-queue can't take the
        no-op path against an output built from now-stale bytes.

        Returns ``None`` (record no meta → no fast path) for an archive-member
        source, when there is no pre-conversion snapshot, when the source moved
        since planning, or when the output can't be fingerprinted.
        """
        if "::" in job.file_path:
            # Archive members: the on-disk "source" is the whole archive and the
            # extracted member is transient, so a reuse can't be proven cheaply.
            return None
        pre = self._delete_plans.get(job.id)
        if not pre or not pre.get("fingerprints"):
            return None
        source_fp = self._norm_fps(pre.get("fingerprints"))
        if not source_fp or self._source_fingerprint(job.file_path) != source_fp:
            return None
        output_fp = self._output_fingerprint(output_path or job.output_path)
        if not output_fp:
            return None
        companions = self._companion_fingerprints(
            output_path or job.output_path, job.mode.value,
        )
        if companions is None:
            return None
        return {
            "mode": job.mode.value,
            "compression": job.compression,
            "split": bool(job.split),
            "source": source_fp,
            "output": output_fp,
            # Every companion output (extractcd's .bin) as it was checked: a
            # retried delete re-checks them too.
            "companions": companions,
        }

    @classmethod
    def _companion_fingerprints(
        cls, output_path: str, mode: str,
    ) -> Optional[Dict[str, Dict[str, int]]]:
        """``{path: fingerprint}`` of the mode's companion outputs beside
        *output_path* (the registry's ``companion_outputs``), or None when one
        cannot be fingerprinted or the mode is unknown. Blocking."""
        try:
            paths = registry.for_mode(mode).companion_outputs(output_path, mode)
        except (KeyError, OSError):
            return None
        out: Dict[str, Dict[str, int]] = {}
        for path in paths:
            fingerprint = cls._output_fingerprint(path)
            if fingerprint is None:
                return None
            out[path] = fingerprint
        return out

    @staticmethod
    def _verifier_identity(job: ConversionJob) -> Dict[str, object]:
        """The mode and tool that produced the job's output."""
        identity: Dict[str, object] = {"mode": job.mode.value}
        try:
            identity["tool"] = registry.for_mode(job.mode.value).id
        except KeyError:
            pass
        return identity

    def _produced_meta_matches(self, job: ConversionJob, meta: object) -> bool:
        """Whether the on-disk output is the *exact* result of THIS request.

        Requires the recorded producing settings (mode + compression + split) to
        equal the current request's, the complete source set to be byte-for-byte
        unchanged since it was verified (so a different track can't be silently
        reused), and the output to be unchanged (so a replaced/corrupted file
        with a fresh mtime can't pass). Any mismatch → re-convert.
        """
        if not isinstance(meta, dict):
            return False
        if not meta.get("output") or not meta.get("source"):
            # Only the verifier identity was recorded (no delete snapshot):
            # nothing proves the output is this request's exact result.
            return False
        if (
            meta.get("mode") != job.mode.value
            or meta.get("compression") != job.compression
            or bool(meta.get("split")) != bool(job.split)
        ):
            return False
        if self._output_fingerprint(job.output_path) != meta.get("output"):
            return False
        if self._source_fingerprint(job.file_path) != meta.get("source"):
            return False
        return True

    async def _output_already_verified(self, job: ConversionJob) -> bool:
        """Whether a prior run already produced and verified this exact output.

        Backs the job-start "recognize prior success" fast path (issue #184,
        site 1): a re-queued job whose verified artifact is already on disk
        completes as a no-op instead of re-spawning the converter. Only a
        verification record carrying a full ``produced_meta`` snapshot (written
        by a prior delete-on-verify conversion) can qualify, and only when that
        snapshot proves the on-disk output is the exact result of the current
        request — see :meth:`_produced_meta_matches`. Directory-input and
        delete-on-verify *requests* always take the normal path (the former has
        split-set outputs, the latter must run its guarded source deletion).
        """
        if (
            job.delete_on_verify
            or job.input_kind == InputKind.DIRECTORY
            or not job.output_path
        ):
            return False
        try:
            record = await verification_store.get_record(job.output_path)
        except Exception as exc:
            # Never let a verification-store hiccup skip a conversion: on any
            # lookup failure fall through to the normal path (re-convert).
            logger.debug(
                "Prior-success check skipped for %s: %s", job.output_path, exc,
            )
            return False
        if not record:
            return False
        meta = record.get("produced_meta")
        if not meta:
            return False
        return await run_in_threadpool(self._produced_meta_matches, job, meta)

    @staticmethod
    async def _stored_check(output_path: str) -> str:
        """The check a stored output passed; "intact" when unknown."""
        try:
            record = await verification_store.get_record(output_path)
        except Exception as exc:
            logger.debug("Stored check lookup failed for %s: %s", output_path, exc)
            return "intact"
        check = (record or {}).get("check_passed")
        return check if check in _CHECK_LABELS else "intact"

    async def _run_job(self, job_id: str):
        try:
            await self._process_job(job_id)
        finally:
            self._semaphore.release()

    async def _check_delete_paths(
        self, job_id: str, paths: list[str], fingerprints: dict, output_real: str,
        probe_seconds: Optional[float] = None,
    ) -> None:
        """Raise RuntimeError unless every path is still safe to delete.

        Lock, Volume, other-job, symlink, type, not-the-output, and the
        snapshot's size / mtime / inode / device. Run before the Referrer hook
        and again after it (#295), since the hook awaits a worker thread.

        A path in this job's own delete hold (``_delete_holds``, #306) skips
        the lock probe: its exclusive lock is this job's, and while it is held
        no other job or process can lock that path. Any other locked path is
        refused, and so is a numbered split part whose logical owner another
        job writes (``mutation_guard``).

        Every probe runs on a detached thread under ``_CANONICAL_PROBE_SECONDS``:
        a volume that stops answering refuses the
        delete instead of stalling the event loop.
        """
        own_holds = set(self._delete_holds.get(job_id, ()))
        jobs = list(self.jobs.values())
        problem = await self._bounded_delete_probe(
            self._delete_path_problem, job_id, paths, fingerprints, output_real,
            own_holds, jobs, seconds=probe_seconds,
        )
        if problem:
            raise RuntimeError(problem)

    @staticmethod
    async def _bounded_delete_probe(
        func: Callable, *args: object, seconds: Optional[float] = None,
    ) -> object:
        """A delete-step probe on a detached thread, bounded (``seconds``, else
        ``_CANONICAL_PROBE_SECONDS``); a timeout raises RuntimeError, so the
        delete is refused and nothing more is removed."""
        try:
            return await run_detached_bounded(
                func, *args,
                timeout=_CANONICAL_PROBE_SECONDS if seconds is None else seconds,
            )
        except DeadlineExpired:
            raise RuntimeError(
                "The Source's volume did not answer; refusing to delete"
            ) from None

    @classmethod
    async def _image_library(cls, path: str, probe_seconds: Optional[float]):
        """The Library holding *path*, for the Referrer hook (#383); None when
        none does. *path* is the Source as the job was given it, never
        resolved: a file listed in a Library's folders is that Library's,
        wherever a symlinked folder leads (#413). A delete-step probe: when it
        can't tell (a timeout, the Library list unreadable) it raises
        RuntimeError, so nothing is removed. Listed in one Library and living
        in another, it answers ``referrers.UndecidedLibrary``: the hook then
        keeps the Source and says why."""
        # Imported here: services.libraries imports this module.
        from services import libraries

        try:
            return await cls._bounded_delete_probe(
                libraries.library_for, path, seconds=probe_seconds,
            )
        except RuntimeError:
            raise
        except libraries.AmbiguousLibrary as exc:
            return referrers.UndecidedLibrary(path, str(exc))
        except Exception as exc:
            raise RuntimeError(
                f"Couldn't tell which Library holds the Source ({exc}); refusing to delete"
            ) from exc

    def _delete_path_problem(
        self, job_id: str, paths: list[str], fingerprints: dict, output_real: str,
        own_holds: Set[str], jobs: List[ConversionJob],
    ) -> Optional[str]:
        """Why the first unsafe path of *paths* can't be deleted, or None.
        Blocking: :meth:`_check_delete_paths` runs it off the event loop."""
        for path in paths:
            if path not in own_holds and locked_for_mutation(path, ignore=own_holds):
                return "Delete path is locked by an active conversion"
            if not is_within_configured_volumes(path, treat_archives=False):
                return "Delete path outside configured volumes; refusing to delete"
            if self._is_path_in_use_by_other_job(job_id, path, jobs):
                return "Delete path is still in use by another job"
            if os.path.islink(path):
                return "Delete path is a symlink; refusing to delete"
            try:
                st = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                return "Delete path no longer exists; refusing to delete"
            if not os.path.isfile(path):
                return "Delete path is not a file; refusing to delete"
            if os.path.realpath(path) == output_real:
                return "Delete path matches output path; refusing to delete"
            fp = fingerprints.get(path)
            if not fp:
                return "Delete fingerprint missing; refusing to delete"
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
            if (
                int(fp.get("size", -1)) != int(st.st_size)
                or int(fp.get("mtime_ns", -1)) != mtime_ns
            ):
                return "Delete path fingerprint mismatch; refusing to delete"
            snapshot_inode = int(fp.get("inode", 0) or 0)
            snapshot_device = int(fp.get("device", 0) or 0)
            current_inode = int(getattr(st, "st_ino", 0) or 0)
            current_device = int(getattr(st, "st_dev", 0) or 0)
            if (
                snapshot_inode > 0 and current_inode > 0
                and snapshot_inode != current_inode
            ) or (
                snapshot_device > 0 and current_device > 0
                and snapshot_device != current_device
            ):
                return "Delete path fingerprint mismatch; refusing to delete"
        return None

    @staticmethod
    def _delete_order(
        file_path: str, output_path: str, expected_paths: List[str],
    ) -> Tuple[str, str, List[str]]:
        """``(output realpath, the Source to name to Referrers, removal
        order)``: the Source's own file last. Blocking (it resolves paths)."""
        output_real = os.path.realpath(output_path)
        source_for_delete = (
            strip_archive_path(file_path) if "::" in file_path else file_path
        )
        source_real = os.path.realpath(source_for_delete)
        delete_order = [p for p in expected_paths if os.path.realpath(p) != source_real]
        if source_real in set(expected_paths):
            delete_order.append(source_real)
        else:
            delete_order = list(expected_paths)
        return output_real, source_for_delete, delete_order

    async def _delete_source_set(
        self,
        owner_id: str,
        *,
        file_path: str,
        output_path: str,
        expected_paths: List[str],
        fingerprints: dict,
        check_label: str,
        cancel_event: asyncio.Event,
        cancelled: Callable[[], bool],
        deleted: List[str],
        probe_seconds: Optional[float] = None,
        travelling_locks: Optional["HeldLocks"] = None,
        planned: Optional[List[str]] = None,
        handed_off: Optional[List[bool]] = None,
        placement: referrers.StagedPlacement | None = None,
        hook_records: list[dict] | None = None,
    ) -> _SourceDelete:
        """The delete step, shared by a job and a retried delete (#329).

        *hook_records* gets the Referrer hook's records as soon as it returns,
        so a caller keeps them even when a later step raises.

        *placement* is the job's output, still staged for a Referrer that
        places it itself (the RomM swap, #391). Whatever the hook did not
        place is placed right after it, before anything is removed (a raise
        there removes nothing). Source files a Referrer removed itself
        (``ReferrerOutcome.removed``) are recorded as deleted and not removed
        again.

        *handed_off* gets ``True`` appended when the unlinks were handed to
        their worker while still running: the outcome is then unknown, and
        ``unfinished_delete(..., in_flight=True)`` takes the delete as started.

        Called holding every path of *expected_paths* exclusively
        (``_delete_holds[owner_id]``); lets go of them before it returns. The
        plan is built again and must name the same paths; each path is checked
        (:meth:`_check_delete_paths`) before the Referrer hook and again after
        it; then the paths are removed, each appended to *deleted* as it goes.
        Raises RuntimeError when the plan, or a path before the hook, is no
        longer safe: nothing is removed. A cancel during the hook, or a
        Referrer or path check that fails after it, keeps the Source with a
        reason; a cancel at the last look, before any unlink, is reported as
        ``cancelled``.

        *probe_seconds* bounds every stage that touches the filesystem: the
        plan, the path resolution and checks, the Referrer hook, each unlink
        and the store clears. A retry passes ``KEPT_DELETE_PROBE_SECONDS``;
        a job leaves it None (the probes take ``_CANONICAL_PROBE_SECONDS``, the
        hook the conversion's stall bound, the unlinks no bound, as before).

        Each unlink is a mutation, never abandoned with its locks
        (``run_detached_mutation``): when the caller stops
        waiting while one still runs (the bound, a cancel), the delete holds,
        and the caller's *travelling_locks* (its output locks), pass to
        its worker, which releases them once the unlink has finished, and
        :class:`MutationInFlight` (or the cancel) propagates.
        """
        try:
            output_real, source_for_delete, delete_order, library = await self._prepare_delete(
                owner_id, file_path, output_path, expected_paths, fingerprints, probe_seconds,
            )
        except BaseException:
            # Refused before the Referrer hook took the staged output: it was
            # checked, so it is placed, never discarded with its slot because
            # the Source side failed.
            if placement is not None:
                await self._place_checked_output(placement)
            raise

        # Referrers (.m3u, ...) move to the output before the Source goes; if
        # any can't, the Source stays (#295). The Image's Library decides
        # which opt-in Referrers run (#383).
        unawaited: list[referrers.HandedOff] = []
        try:
            referrer_outcome = await referrers.update_before_source_removal(
                source_for_delete,
                {path: output_path for path in delete_order},
                library=library,
                cancel_event=cancel_event,
                bound=probe_seconds,
                placement=placement,
                unawaited=unawaited,
            )
        except asyncio.CancelledError:
            # A shutdown cancelled this job mid-hook while an apply (the RomM
            # swap's undo) still runs: it owns the holds from here, as an
            # abandoned one does below.
            if unawaited:
                self._hand_holds_to(
                    referrers.ReferrerOutcome(lingering=[h.task for h in unawaited]),
                    owner_id, travelling_locks,
                )
                # What each one did, when it ends, still reaches the job: a
                # Source the swap removed is recorded as deleted, with its
                # record.
                for handed in unawaited:
                    handed.task.add_done_callback(self._take_late_outcome(
                        handed, deleted, hook_records, owner_id,
                    ))
            raise
        if hook_records is not None:
            hook_records[:] = referrer_outcome.records
        if (
            placement is not None and not referrer_outcome.lingering
            and not placement.held.is_set()
        ):
            # The output is placed whatever the hook did (a kept Source keeps
            # its output beside it), and before any unlink below. Not when a
            # Referrer stopped part way and holds it (#391): the name may be
            # the Source's.
            await placement.place()
        if referrer_outcome.removed:
            # A Referrer replaced the Source and removed it itself (#391).
            removed = set(referrer_outcome.removed)
            deleted.extend(p for p in delete_order if p in removed)
            expected_paths = [p for p in expected_paths if p not in removed]
            delete_order = [p for p in delete_order if p not in removed]
        if referrer_outcome.lingering:
            # An async apply was cancelled and is still cleaning up: like a
            # mutation's worker, it owns the delete holds
            # and the travelling locks from here, let go when it ends. The
            # outcome failed, so the Source is kept below, unless the apply
            # then finishes: what it did is taken when it ends (the RomM
            # swap's delete of the parked Source), into
            # the records this job keeps.
            self._hand_holds_to(referrer_outcome, owner_id, travelling_locks)
            for handed in referrer_outcome.handed_off:
                handed.task.add_done_callback(self._take_late_outcome(
                    handed, deleted, referrer_outcome.records, owner_id,
                ))
        outcome = _SourceDelete(
            referrer_records=referrer_outcome.records, notes=list(referrer_outcome.notes),
        )
        if referrer_outcome.removed and not delete_order:
            # Nothing is left to remove: the Source is gone, so a late cancel
            # changes nothing.
            if planned is not None:
                planned[:] = list(deleted)
            await self._release_delete_holds_in_background(owner_id, probe_seconds)
            await self._clear_source_records(file_path, probe_seconds)
            return outcome
        # The hook awaited a worker thread: re-ask cancel and every per-path
        # identity check before removing.
        skipped = f"Output is {check_label}. Delete skipped (cancelled)."
        if cancel_event.is_set():
            outcome.kept_reason = skipped
        elif not referrer_outcome.ok:
            outcome.kept_reason = referrer_outcome.kept_message
        else:
            try:
                await self._check_delete_paths(
                    owner_id, expected_paths, fingerprints, output_real, probe_seconds,
                )
            except RuntimeError as exc:
                outcome.kept_reason = f"converted, source kept: {exc}"
            # That check awaited too; a Cancel there still wins.
            if outcome.kept_reason is None and cancel_event.is_set():
                outcome.kept_reason = skipped
        if outcome.kept_reason and referrer_outcome.handed_off:
            # Not "definitely kept" while a handed-off apply may still remove
            # it: the job is updated when it ends.
            names = ", ".join(sorted({h.referrer_id for h in referrer_outcome.handed_off}))
            outcome.kept_reason = (
                f"{outcome.kept_reason}; {names} is still finishing, so the source "
                "may yet be removed (this job is updated when it ends)"
            )
        if outcome.kept_reason:
            await self._release_delete_holds_in_background(owner_id, probe_seconds)
            return outcome
        if cancel_event.is_set() or cancelled():
            await self._release_delete_holds_in_background(owner_id, probe_seconds)
            outcome.cancelled = True
            return outcome
        if planned is not None:
            planned[:] = delete_order
        # The whole set is one mutation: a caller that stops
        # waiting hands the rest of the plan to the worker with the locks, and
        # the worker removes every planned file before it lets them go.
        await run_detached_mutation(
            _remove_in_order, delete_order, deleted, timeout=probe_seconds,
            hand_off=lambda: self._hand_off_locks(
                owner_id, travelling_locks, handed_off=handed_off,
            ),
        )
        await self._release_delete_holds_in_background(owner_id, probe_seconds)
        await self._clear_source_records(file_path, probe_seconds)
        return outcome

    def _take_late_outcome(
        self, handed: referrers.HandedOff, deleted: list[str],
        records: list[dict] | None, job_id: str,
    ) -> Callable[[asyncio.Future], None]:
        """A done callback that records what an apply handed off at shutdown
        did once it ends: the Source files it removed go to *deleted*, its
        Ledger record to *records*, and the amend listeners are told, so a
        Ledger row written when the job ended is updated too. Runs on the loop; never raises."""

        def ended(_task: asyncio.Future) -> None:
            outcome = handed.outcome()
            if outcome is None:
                return
            deleted.extend(p for p in outcome.removed if p not in deleted)
            if records is not None:
                for record in outcome.records:
                    # It replaces the "abandoned" record the hook wrote for
                    # it, if any.
                    at = next((
                        i for i, old in enumerate(records)
                        if old.get("status") == "abandoned"
                        and old.get("referrer") == record.get("referrer")
                        and old.get("target") == record.get("target")
                    ), None)
                    if at is None:
                        records.append(record)
                    else:
                        records[at] = record
            logger.info(
                "A %s update handed off at shutdown ended: %s", handed.referrer_id,
                ", ".join(outcome.removed) or "nothing removed",
            )
            # Still listed, or already archived (a retry's owner is no job).
            job = self.jobs.get(job_id) or (self._archived_jobs.get(job_id) or (None,))[0]
            if job is None:
                return
            if outcome.removed:
                note = f"{handed.referrer_id} finished after all and removed the source"
                job.message = f"{job.message}; {note}" if job.message else note
            self._notify_amended(job)

        return ended

    def _hand_holds_to(
        self, outcome: referrers.ReferrerOutcome, owner_id: str,
        travelling_locks: Optional["HeldLocks"],
    ) -> None:
        """Hand *owner_id*'s delete holds and the travelling locks to the
        applies still running in *outcome* (``lingering``): they are let go
        only once the last one ends (``ReferrerOutcome.outlive``)."""
        held = [
            *self._delete_holds.pop(owner_id, []),
            *(travelling_locks.take() if travelling_locks is not None else []),
        ]
        outcome.outlive(lambda: self._spawn_background(self._let_go_in_background(held)))

    async def _prepare_delete(
        self, owner_id: str, file_path: str, output_path: str,
        expected_paths: list[str], fingerprints: dict, probe_seconds: float | None,
    ) -> tuple[str, str, list[str], object]:
        """The delete step up to the Referrer hook: the plan rebuilt and
        compared, the removal order, every path checked, and the Image's
        Library. ``(output realpath, the Source named to Referrers, removal
        order, Library)``. Raises RuntimeError when any of it no longer holds:
        nothing is removed."""
        current_plan = await self._bounded_delete_probe(
            build_delete_plan, file_path, seconds=probe_seconds,
        )
        if (
            current_plan.get("errors")
            or current_plan.get("unsafe_paths")
            or current_plan.get("missing_paths")
        ):
            raise RuntimeError("Delete plan no longer safe; refusing to delete")
        if set(current_plan.get("delete_paths", [])) != set(expected_paths):
            raise RuntimeError("Delete plan changed; refusing to delete")

        output_real, source_for_delete, delete_order = await self._bounded_delete_probe(
            self._delete_order, file_path, output_path, list(expected_paths),
            seconds=probe_seconds,
        )
        await self._check_delete_paths(
            owner_id, expected_paths, fingerprints, output_real, probe_seconds,
        )
        library = await self._image_library(source_for_delete, probe_seconds)
        return output_real, source_for_delete, delete_order, library

    @staticmethod
    async def _place_checked_output(placement: referrers.StagedPlacement) -> None:
        """Place a checked output still staged, when the delete step stopped
        before a Referrer could. Logged, never raised: the step's own error
        is the one that counts."""
        try:
            await placement.place()
        except Exception:
            logger.warning("Could not place the checked output", exc_info=True)

    async def _clear_source_records(
        self, file_path: str, probe_seconds: Optional[float],
    ) -> None:
        """Drop the check and metadata records of a Source just deleted."""
        ext = os.path.splitext(file_path)[1].lower()
        clears = []
        if ext in registry.verify_extensions():
            clears.append((verification_store.clear, verification_store.clear_blocking))
        if ext == ".chd":
            clears.append((chd_metadata_store.clear, chd_metadata_store.clear_blocking))
        for clear, clear_blocking in clears:
            if probe_seconds is None:
                await clear(file_path)
                continue
            try:
                await self._bounded_delete_probe(
                    clear_blocking, file_path, seconds=probe_seconds,
                )
            except RuntimeError:
                # The Source is gone; a stale record of it is all this leaves.
                logger.warning(
                    "Clearing the records of deleted %s did not finish", file_path,
                    exc_info=True,
                )

    @staticmethod
    async def _let_go_in_background(
        paths: Iterable[str], seconds: Optional[float] = None,
    ) -> None:
        """Release exclusive locks on a detached worker; wait for it at most
        *seconds* (``KEPT_DELETE_PROBE_SECONDS`` by default), then move on.

        Letting go of a lock is lock-file I/O (``_unlink_then_close``), which a
        lock directory that stops answering can stall: never on the event
        loop, never on the shared threadpool, never unbounded under
        ``_sweep_lock``. The worker owns the release once
        started and finishes on its own, closing every handle, whether or not
        the caller still waits. The one path a job and a retry both let go by.

        The worker starts before this first awaits (``start_detached``,
        CodeRabbit outside-diff on job_manager.py:3362): the caller has
        already taken *paths* out of its own ownership, so a cancel landing
        before a lazily started worker ran would leave them held for good.
        A cancel now only stops the waiting.
        """
        paths = list(paths)
        if not paths:
            return
        bound = KEPT_DELETE_PROBE_SECONDS if seconds is None else seconds
        # No detached slot: a thread of its own, still never the pool; no
        # thread at all: tried again until one starts, so the locks are never
        # left held.
        worker = start_detached_eventually(_let_go, paths)
        if worker is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(worker), bound)
        except asyncio.TimeoutError:
            logger.warning(
                "Letting go of %d lock(s) did not finish in %gs; it goes on in "
                "the background", len(paths), bound,
            )

    async def _release_delete_holds_in_background(
        self, owner_id: str, seconds: Optional[float] = None,
    ) -> None:
        await self._let_go_in_background(self._delete_holds.pop(owner_id, []), seconds)

    def _hand_off_locks(
        self, owner_id: str, travelling: Optional[HeldLocks],
        *, handed_off: Optional[List[bool]] = None,
    ) -> Callable[[], None]:
        """Take *owner_id*'s delete holds and the *travelling* locks from their
        owner, for a mutation's worker to release (``run_detached_mutation``).
        Called only for a mutation still running; *handed_off* records that."""
        if handed_off is not None:
            handed_off.append(True)
        paths = [
            *self._delete_holds.pop(owner_id, []),
            *(travelling.take() if travelling is not None else []),
        ]
        return lambda: _let_go(paths)

    def _kept_delete_snapshot(
        self, source_path: str, output_path: str, meta: dict, recorded_source: str,
    ) -> Union[Dict[str, object], str]:
        """What a retried delete of *source_path* stands on, established now:
        its delete snapshot, or why it no longer holds. Blocking.

        Fails closed on each: the Source is inside the configured Volumes, the
        output is the very file its check recorded (``produced_meta``: size,
        mtime, inode, device), and the Source set is byte-for-byte the one
        that output was made and checked from. The record names the Source
        by its real path, so the two are compared resolved: a Library reached
        through a symlink is still the same Source.
        """
        if os.path.realpath(source_path) != recorded_source:
            return "the output's check record names another Source"
        if not is_within_configured_volumes(source_path, treat_archives=False):
            return "the Source is not inside the configured volumes"
        if self._output_fingerprint(output_path) != meta.get("output"):
            return "the output is gone or changed since its check"
        # And every companion output, as the check recorded it: one gone,
        # changed or new means the output is not the checked one.
        if self._companion_fingerprints(
            output_path, str(meta.get("mode") or ""),
        ) != (meta.get("companions") or {}):
            return "a companion output is gone or changed since its check"
        try:
            snapshot = build_delete_snapshot(source_path)
        except (OSError, ValueError) as exc:
            return f"the Source can no longer be planned for deletion: {exc}"
        if not snapshot.get("paths") or self._norm_fps(
            snapshot.get("fingerprints"),
        ) != meta.get("source"):
            return "the Source changed since its output was checked"
        return snapshot

    async def _kept_delete_snapshot_bounded(
        self, source_path: str, output_path: str, meta: dict, recorded_source: str,
    ) -> Union[Dict[str, object], str]:
        """:meth:`_kept_delete_snapshot` on a detached thread, bounded: a
        volume that stops answering is a reason to try again later."""
        try:
            return await run_detached_bounded(
                self._kept_delete_snapshot, source_path, output_path, meta,
                recorded_source, timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except (DeadlineExpired, ReadCancelled):
            return KEPT_DELETE_UNANSWERED

    @contextlib.asynccontextmanager
    async def kept_delete(
        self, *, source_path: str, output_path: str,
        required_check: Optional[str] = None, planned: Optional[List[str]] = None,
        stopping: Callable[[], bool] = lambda: False,
    ):
        """Prepare a retried delete (#329) and yield a :class:`KeptDeleteRun`.

        Under the output's, its companions' and the delete paths' locks, held
        until the block ends, this re-establishes everything the delete stands
        on and re-runs the output's check (:meth:`_recheck_output`).
        ``run.refused`` is the outcome when it no longer holds;
        otherwise ``await run.delete()`` removes the Source. The two are split
        so a caller can run the long check without its own lock and take that
        lock only to delete (the sweep's ``_sweep_lock``). *planned* finishes a
        delete that already started (:meth:`finish_kept_delete`) instead.
        *stopping* is asked before the output's check and before the unlinks:
        once true, the outcome is ``stopped`` and nothing is removed.
        """
        run = KeptDeleteRun()
        owner_id = f"kept-delete:{uuid.uuid4().hex}"
        # The locks this holds. They travel with an unlink that outlives the
        # wait, so they are released only from here.
        travelling = HeldLocks()
        try:
            if planned is None:
                run.refused = await self._prepare_retry(
                    run, owner_id, travelling, source_path, output_path, required_check,
                    stopping,
                )
            else:
                run.refused = await self._prepare_finish(
                    run, owner_id, travelling, source_path, output_path, planned,
                    required_check, stopping,
                )
            yield run
        finally:
            # Off the loop: lock files are I/O.
            holds = self._delete_holds.pop(owner_id, [])
            await self._let_go_in_background([*holds, *travelling.take()])

    async def retry_kept_delete(
        self, *, source_path: str, output_path: str, required_check: Optional[str] = None,
    ) -> KeptDeleteRetry:
        """Retry only the delete step for a Source a reader kept (#329).

        A job whose delete step found another job still reading its Source
        completed and kept it (``SOURCE_KEPT_FOR_READERS``); its output is
        already checked and recorded. This takes the same locks the job held
        (the output's, then every delete path's, exclusively, without
        waiting), re-establishes under them everything the delete stood on
        (:meth:`_kept_delete_snapshot`, plus a passing check record that names
        this Source), re-runs the output's check, then runs the shared delete
        step (:meth:`_delete_source_set`, with its ``mutation_guard`` and
        Referrer hook). Never raises for a refusal: the outcome says whether to
        try again (``reader``, ``kept``) or never (``stale``).

        *required_check* is the check the Rule asks for now (its
        ``effective_check_level``): ``round_trip`` takes a Round-tripped
        record, ``container_only`` an Intact or a Round-tripped one. A record
        that falls short is ``stale``.
        """
        async with self.kept_delete(
            source_path=source_path, output_path=output_path,
            required_check=required_check,
        ) as run:
            return run.refused or await run.delete()

    async def _kept_record(
        self, output_path: str, required_check: Optional[str],
    ) -> Union[KeptDeleteRetry, Tuple[str, dict, dict]]:
        """The output's passing check record, as a retried delete needs it:
        ``(check, meta, record)``, or the outcome that refuses it."""
        try:
            record = await run_detached_bounded(
                verification_store.get_record_blocking, output_path,
                timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except DeadlineExpired:
            return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
        except OSError as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        check = (record or {}).get("check_passed")
        meta = (record or {}).get("produced_meta")
        if (
            not record
            or check not in _CHECK_LABELS
            or not _check_satisfies(check, required_check)
            or not record.get("source_path")
            or not isinstance(meta, dict)
            or not meta.get("source")
            or not meta.get("output")
        ):
            return KeptDeleteRetry(KEPT_STALE, "the output's check record no longer matches")
        return check, meta, record

    async def _take_output(self, output_path: str, travelling: HeldLocks) -> Optional[KeptDeleteRetry]:
        """Take the output's lock for a retried delete, bounded: taking it
        stats the path, and a lock taken after this gave up is let go. None
        once it is held (in *travelling*)."""
        try:
            output_held = await run_detached_undoable(
                lock_manager.acquire_lock, output_path, allow_existing=True,
                timeout=KEPT_DELETE_PROBE_SECONDS,
                undo=lambda held: _let_go([output_path] if held else []),
            )
        except DeadlineExpired:
            return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
        except (LockFailed, OSError) as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        if not output_held:
            return KeptDeleteRetry(KEPT_READER, "the output is in use by another job")
        travelling.add(output_path)
        return None

    async def _recheck_output(
        self, source_path: Optional[str], output_path: str, meta: dict,
        required_check: Optional[str],
    ) -> Optional[KeptDeleteRetry]:
        """Re-run the output's check before a retried delete removes anything.
        None when it passes.

        The record's stat fingerprint only filters: an in-place rewrite of
        the same length with its mtime put back keeps all of it, and the
        app's locks bind no writer outside it. So the authority is the check
        itself, the job's own: the tool's container check
        (:meth:`_container_check`, under the tool's verify bound), plus the
        Round-trip (``round_trip``) when *required_check* asks for it and the
        whole Source is there to compare (*source_path*). A failed check is
        ``stale``; one that reached no verdict is a ``kept`` attempt.
        """
        mode = str(meta.get("mode") or "")
        try:
            tool = registry.for_mode(mode)
        except KeyError:
            return KeptDeleteRetry(KEPT_STALE, f"the output's mode {mode!r} is not known")
        cancel = asyncio.Event()
        try:
            verify_path = await self._container_check(tool, output_path, mode, cancel)
        except VerifyUnanswered as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        except ConversionCancelled as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        except (RuntimeError, OSError) as exc:
            return KeptDeleteRetry(
                KEPT_STALE, f"the output no longer passes its check ({exc})",
            )
        if required_check == RECOGNIZED:
            # The Recognized proof again (#423): the output's bytes, not its
            # record, are what a DAT vouches for.
            bound = await tool.verify_timeout(verify_path, cancel_event=cancel)
            found = await recognize(verify_path, cancel_event=cancel, timeout=bound)
            if found.passed:
                return None
            if found.timed_out or found.cancelled:
                return KeptDeleteRetry(KEPT_KEPT, found.message or "the lookup did not finish")
            return KeptDeleteRetry(
                KEPT_STALE, f"the output is no longer Recognized ({found.message})",
            )
        if required_check != "round_trip" or source_path is None:
            return None
        outcome = await round_trip(tool, source_path, verify_path, cancel_event=cancel)
        if outcome.passed:
            return None
        if outcome.timed_out or outcome.cancelled:
            return KeptDeleteRetry(KEPT_KEPT, outcome.message or "the Round-trip did not finish")
        if check_verdict(outcome.as_verify_result()) is False:
            # The content differs: a real verdict on the output, recorded like
            # a failed container check, so it no longer shows as Round-tripped
            # (#351, CodeRabbit on job_manager.py:3663). The Source stays.
            await verification_store.mark_failed(output_path)
        return KeptDeleteRetry(
            KEPT_STALE, f"the output no longer passes its Round-trip ({outcome.message})",
        )

    async def _prepare_retry(
        self, run: "KeptDeleteRun", owner_id: str, travelling: HeldLocks,
        source_path: str, output_path: str, required_check: Optional[str],
        stopping: Callable[[], bool],
    ) -> Optional[KeptDeleteRetry]:
        """:meth:`kept_delete` for a Source that is whole. Sets ``run``'s delete."""
        if "::" in source_path:
            return KeptDeleteRetry(
                KEPT_STALE, "an archive member's Source cannot be proven unchanged",
            )
        refused = await self._take_output(output_path, travelling)
        if refused is not None:
            return refused
        found = await self._kept_record(output_path, required_check)
        if isinstance(found, KeptDeleteRetry):
            return found
        check, meta, record = found
        # The companion outputs too, as the job held them (#329): nobody may
        # change one while this runs.
        companions = list(meta.get("companions") or {})
        if companions:
            try:
                taken_companions = await run_detached_undoable(
                    self._take_exclusively, companions,
                    timeout=KEPT_DELETE_PROBE_SECONDS, undo=_let_go,
                )
            except DeadlineExpired:
                return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
            except (LockFailed, OSError) as exc:
                return KeptDeleteRetry(KEPT_KEPT, str(exc))
            if taken_companions is None:
                return KeptDeleteRetry(KEPT_READER, "a companion output is in use")
            for path in taken_companions:
                travelling.add(path)
        snapshot = await self._kept_delete_snapshot_bounded(
            source_path, output_path, meta, record["source_path"],
        )
        if snapshot == KEPT_DELETE_UNANSWERED:
            return KeptDeleteRetry(KEPT_KEPT, snapshot)
        if isinstance(snapshot, str):
            return KeptDeleteRetry(KEPT_STALE, snapshot)
        paths = list(snapshot["paths"])
        try:
            taken = await run_detached_undoable(
                self._take_exclusively, paths,
                timeout=KEPT_DELETE_PROBE_SECONDS, undo=_let_go,
            )
        except DeadlineExpired:
            return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
        except (LockFailed, OSError) as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        if taken is None:
            return KeptDeleteRetry(KEPT_READER, SOURCE_KEPT_FOR_READERS)
        self._delete_holds[owner_id] = list(taken)
        # Once more under the holds: nothing in the app can change the set now.
        again = await self._kept_delete_snapshot_bounded(
            source_path, output_path, meta, record["source_path"],
        )
        if isinstance(again, str):
            status = KEPT_KEPT if again == KEPT_DELETE_UNANSWERED else KEPT_STALE
            return KeptDeleteRetry(status, again)
        if stopping():
            return KeptDeleteRetry(KEPT_STOPPED, "stopped before the output's check")
        refused = await self._recheck_output(source_path, output_path, meta, required_check)
        if refused is not None:
            return refused

        async def delete() -> KeptDeleteRetry:
            # The cheap filter once more, right before the unlinks: the check
            # took a while, and the stat fingerprint still catches a change.
            last = await self._kept_delete_snapshot_bounded(
                source_path, output_path, meta, record["source_path"],
            )
            if isinstance(last, str):
                status = KEPT_KEPT if last == KEPT_DELETE_UNANSWERED else KEPT_STALE
                return KeptDeleteRetry(status, last)
            deleted: List[str] = []
            planned: List[str] = []
            handed_off: List[bool] = []
            try:
                removal = await self._delete_source_set(
                    owner_id,
                    file_path=source_path,
                    output_path=output_path,
                    expected_paths=paths,
                    fingerprints=snapshot.get("fingerprints") or {},
                    check_label=_CHECK_LABELS[check],
                    cancel_event=asyncio.Event(),
                    # A stop seen at the last look keeps the Source whole.
                    cancelled=stopping,
                    deleted=deleted,
                    probe_seconds=KEPT_DELETE_PROBE_SECONDS,
                    travelling_locks=travelling,
                    planned=planned,
                    handed_off=handed_off,
                )
            except MutationInFlight as exc:
                # Its locks went with it, and the rest of the plan; the worker
                # removes every planned file, then lets them go.
                rest = unfinished_delete(planned, deleted, in_flight=True)
                return KeptDeleteRetry(
                    KEPT_UNKNOWN, str(exc), tuple(deleted), tuple(rest or planned),
                )
            except asyncio.CancelledError:
                # Cancelled mid-plan: what
                # started, or is still running on the worker, is recorded by
                # the caller from `run.started`; then this goes on.
                rest = unfinished_delete(planned, deleted, in_flight=bool(handed_off))
                if rest:
                    run.started = KeptDeleteRetry(
                        KEPT_UNKNOWN, "the delete was cancelled", tuple(deleted), tuple(rest),
                    )
                raise
            except (RuntimeError, OSError) as exc:
                logger.warning(
                    "Retrying the delete of %s kept it", source_path, exc_info=True,
                )
                rest = unfinished_delete(planned, deleted)
                if rest:
                    # Started, then refused part way: the
                    # rest is finished on the next sweep, never dropped.
                    return KeptDeleteRetry(KEPT_STARTED, str(exc), tuple(deleted), tuple(rest))
                # A refusal before any file went: an attempt.
                return KeptDeleteRetry(KEPT_KEPT, str(exc), tuple(deleted))
            if removal.cancelled:
                return KeptDeleteRetry(KEPT_STOPPED, "stopped before the unlinks")
            if removal.kept_reason:
                return KeptDeleteRetry(KEPT_KEPT, removal.kept_reason)
            # What a Referrer had to say (the RomM swap that couldn't run
            # after the output was placed, #391) is the outcome's reason.
            return KeptDeleteRetry(
                KEPT_DELETED, "; ".join(removal.notes) or None, tuple(deleted),
            )

        run.set_delete(delete)
        return None

    # ------------------------------------------------------------------
    # Deleting duplicate copies (#389)
    # ------------------------------------------------------------------

    @staticmethod
    def _copy_snapshot(path: str, identity: Tuple[int, int]) -> Union[Dict[str, object], str]:
        """*path*'s delete snapshot, or why it may not be deleted. Blocking.

        The shared delete plan and its fingerprints (``build_delete_snapshot``),
        and the copy's own file must still be the one the Finding showed: its
        size and mtime (*identity*). Anything else is a reason to refuse.
        """
        try:
            snapshot = build_delete_snapshot(path)
        except (OSError, ValueError) as exc:
            return f"it can no longer be planned for deletion: {exc}"
        own = (snapshot.get("fingerprints") or {}).get(os.path.realpath(path)) or {}
        if (own.get("size"), own.get("mtime_ns")) != tuple(identity):
            return "it changed since the Finding was recorded"
        return snapshot

    async def _copy_snapshot_bounded(
        self, path: str, identity: Tuple[int, int],
    ) -> Union[Dict[str, object], str]:
        try:
            return await run_detached_bounded(
                self._copy_snapshot, path, identity, timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except (DeadlineExpired, ReadCancelled):
            return KEPT_DELETE_UNANSWERED

    @staticmethod
    def _open_keeper(path: str) -> Tuple[int, Tuple[int, int, int, int]]:
        """Open *path* to read, and its ``(device, inode, size, mtime_ns)``
        through that descriptor. Blocking. The caller closes the descriptor."""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            st = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    @staticmethod
    def _keeper_moved(path: str, fd: int, snapshot: Tuple[int, int, int, int]) -> bool:
        """Whether *path* no longer names the file *fd* was opened on, as it
        was then (device, inode, size, mtime). Blocking. Unreadable is moved."""
        try:
            held = os.fstat(fd)
            now = os.stat(path)
        except OSError:
            return True
        for st in (held, now):
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != snapshot:
                return True
        return False

    async def _keeper_still_held(
        self, keeper: str, fd: int, snapshot: Tuple[int, int, int, int],
        travelling: HeldLocks,
    ) -> Optional[str]:
        """Why the batch must stop before its next delete, or None (#389).

        The keeper's lock must still be this batch's (a lingering Referrer
        operation may have taken it), and its path must still name the file
        that was hashed (held open as *fd*): same device, inode, size and
        mtime. That catches a rename swap; an in-place rewrite by a writer
        with write access to the keeper cannot be caught by any ordering here,
        since the same writer could corrupt it right after the cleanup.
        """
        if not travelling.holds(keeper):
            return (
                "the copy to keep's lock went with an unfinished playlist or "
                "gamelist update; nothing more is deleted"
            )
        try:
            moved = await run_detached_bounded(
                self._keeper_moved, keeper, fd, snapshot, timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except (DeadlineExpired, ReadCancelled):
            return "the copy to keep did not answer; nothing more is deleted"
        if moved:
            return "the copy to keep was replaced or changed; nothing more is deleted"
        return None

    async def _keeper_problem(
        self, keeper: str, keeper_identity: Tuple[int, int], keeper_key: str, fd: int,
        snapshot: Tuple[int, int, int, int], *, cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[str]:
        """Why *keeper* may not be kept now, or None. Called under its lock (#389).

        Its size and mtime must still be the Finding's (*snapshot*, taken
        through *fd* when it was opened), and then its content key is computed
        again from the bytes it holds now (``content_keys.recompute``: an image
        hashed whole through *fd*, or its tool's full pass), never the stored
        key, and must be the group's: size and mtime say nothing about bytes
        written back with them. The path must still name *fd*'s file, unchanged,
        after that read. Any read that fails, stalls or is cancelled refuses.
        """
        from services import content_keys

        if (snapshot[2], snapshot[3]) != tuple(keeper_identity):
            return "the copy to keep changed since the Finding was recorded"
        try:
            key = await content_keys.recompute(keeper, fd=fd, cancel_event=cancel_event)
        except content_keys.KeyUnavailable as exc:
            return f"the copy to keep could not be read in full ({exc})"
        except (ReadCancelled, StorageAbandoned) as exc:
            return f"the copy to keep could not be read in full ({exc})"
        if key is None:
            return "the copy to keep has no content hash that can be read in full"
        if key != keeper_key:
            return "the copy to keep no longer holds this disc (its bytes changed)"
        try:
            moved = await run_detached_bounded(
                self._keeper_moved, keeper, fd, snapshot, timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except (DeadlineExpired, ReadCancelled):
            return "the copy to keep did not answer"
        if moved:
            return "the copy to keep changed while it was read"
        return None

    async def delete_duplicate_copies(
        self, *, keeper: str, keeper_identity: Tuple[int, int], keeper_key: str,
        copies: List[Tuple[str, List[str], Tuple[int, int]]],
        copy_problem: Optional[Callable[[str], Optional[str]]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[List[Tuple[str, "KeptDeleteRetry"]], Optional[str]]:
        """Delete the duplicate copies of a group, keeping *keeper* (#389).

        Each copy is ``(path, the delete paths the user confirmed, the
        identity the Finding showed)``. The keeper's lock is taken first and
        held throughout, so nothing in the app writes it while its copies go;
        it is opened and held open to the end, and must still be the file the
        Finding showed, holding *keeper_key* by its bytes read now through that
        descriptor (:meth:`_keeper_problem`). Before each copy, the lock must
        still be this batch's and the path must still name the held file
        (:meth:`_keeper_still_held`); any change stops the batch, and what is
        left stays. Then each copy goes through the job's own delete step
        (:meth:`_delete_source_set`), as a retried delete does: its delete
        paths taken exclusively without waiting, its snapshot taken again
        under them and compared, *copy_problem* asked again under them (a sheet
        that names it since the plan), the per-path checks, the Referrer hook
        (a playlist naming the copy is pointed at the keeper), then the unlinks
        as one mutation.

        *cancel_event* (the delete job's) stops the keeper's read, and keeps
        every copy not yet started.

        Returns each copy's outcome (:class:`KeptDeleteRetry`; a refusal
        deletes nothing of that copy) and, when the keeper no longer looked
        like the file that was hashed once the last delete was done, why.
        """
        travelling = HeldLocks()
        results: List[Tuple[str, KeptDeleteRetry]] = []
        fd: Optional[int] = None
        try:
            refused = await self._take_output(keeper, travelling)
            snapshot: Tuple[int, int, int, int] = (0, 0, 0, 0)
            if refused is None:
                try:
                    # A descriptor that arrives after the bound gave up is
                    # closed by the undo, never left open.
                    fd, snapshot = await run_detached_undoable(
                        self._open_keeper, keeper, timeout=KEPT_DELETE_PROBE_SECONDS,
                        undo=_close_opened,
                    )
                except (DeadlineExpired, ReadCancelled):
                    refused = KeptDeleteRetry(KEPT_KEPT, "the copy to keep did not answer")
                except OSError as exc:
                    refused = KeptDeleteRetry(
                        KEPT_STALE, f"the copy to keep could not be opened ({exc.strerror or exc})",
                    )
            if refused is None:
                problem = await self._keeper_problem(
                    keeper, keeper_identity, keeper_key, fd, snapshot,
                    cancel_event=cancel_event,
                )
                if problem:
                    refused = KeptDeleteRetry(KEPT_STALE, problem)
            if refused is not None:
                return [(path, refused) for path, _paths, _identity in copies], None
            for path, expected, identity in copies:
                stop = await self._keeper_still_held(keeper, fd, snapshot, travelling)
                if stop is None and cancel_event is not None and cancel_event.is_set():
                    stop = "the delete was cancelled"
                if stop is None:
                    outcome = await self._delete_copy(
                        path, keeper, expected, identity, travelling, copy_problem,
                    )
                    results.append((path, outcome))
                    if outcome.status != KEPT_UNKNOWN:
                        continue
                    # An unlink outlived the wait and took the locks with it,
                    # the keeper's included: no further copy goes without them.
                    stop = "an earlier delete was still running"
                for rest_path, _paths, _identity in copies[len(results):]:
                    results.append((rest_path, KeptDeleteRetry(KEPT_KEPT, stop)))
                break
            after = None
            if results and any(o.status == KEPT_DELETED for _p, o in results):
                after = await self._keeper_still_held(keeper, fd, snapshot, travelling)
                if after:
                    logger.warning(
                        "duplicate delete: the copy to keep %s changed during the batch: %s",
                        keeper, after,
                    )
            return results, after
        finally:
            if fd is not None:
                held_fd = fd
                try:
                    await run_detached_bounded(
                        os.close, held_fd, timeout=KEPT_DELETE_PROBE_SECONDS,
                    )
                except (DeadlineExpired, ReadCancelled, OSError):
                    logger.warning("duplicate delete: closing %s did not finish", keeper)
            await self._let_go_in_background(travelling.take())

    async def _delete_copy(
        self, path: str, keeper: str, expected: List[str], identity: Tuple[int, int],
        travelling: HeldLocks,
        copy_problem: Optional[Callable[[str], Optional[str]]] = None,
    ) -> "KeptDeleteRetry":
        """One copy of :meth:`delete_duplicate_copies`, under its own holds."""
        owner_id = f"duplicate-delete:{uuid.uuid4().hex}"
        try:
            snapshot = await self._copy_snapshot_bounded(path, identity)
            if isinstance(snapshot, str):
                status = KEPT_KEPT if snapshot == KEPT_DELETE_UNANSWERED else KEPT_STALE
                return KeptDeleteRetry(status, snapshot)
            paths = list(snapshot["paths"])
            if sorted(paths) != sorted(expected):
                return KeptDeleteRetry(KEPT_STALE, "its delete plan changed since it was shown")
            try:
                taken = await run_detached_undoable(
                    self._take_exclusively, paths,
                    timeout=KEPT_DELETE_PROBE_SECONDS, undo=_let_go,
                )
            except DeadlineExpired:
                return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
            except (LockFailed, OSError) as exc:
                return KeptDeleteRetry(KEPT_KEPT, str(exc))
            if taken is None:
                return KeptDeleteRetry(KEPT_READER, "it is in use by a job")
            self._delete_holds[owner_id] = list(taken)
            # Once more under the holds: nothing in the app can change it now.
            again = await self._copy_snapshot_bounded(path, identity)
            if isinstance(again, str) or self._norm_fps(again.get("fingerprints")) != (
                self._norm_fps(snapshot.get("fingerprints"))
            ):
                return KeptDeleteRetry(KEPT_STALE, "it changed while it was being deleted")
            if copy_problem is not None:
                # Asked again under the holds, right before the delete: a sheet
                # that names the copy since the plan keeps it (#389).
                try:
                    problem = await run_detached_bounded(
                        copy_problem, path, timeout=KEPT_DELETE_PROBE_SECONDS,
                    )
                except (DeadlineExpired, ReadCancelled):
                    return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
                if problem:
                    return KeptDeleteRetry(KEPT_STALE, problem)
            deleted: List[str] = []
            planned: List[str] = []
            handed_off: List[bool] = []
            try:
                removal = await self._delete_source_set(
                    owner_id, file_path=path, output_path=keeper, expected_paths=paths,
                    fingerprints=snapshot.get("fingerprints") or {}, check_label="kept",
                    cancel_event=asyncio.Event(), cancelled=lambda: False,
                    deleted=deleted, probe_seconds=KEPT_DELETE_PROBE_SECONDS,
                    travelling_locks=travelling, planned=planned, handed_off=handed_off,
                )
            except MutationInFlight as exc:
                rest = unfinished_delete(planned, deleted, in_flight=True)
                return KeptDeleteRetry(
                    KEPT_UNKNOWN, str(exc), tuple(deleted), tuple(rest or planned),
                )
            except (RuntimeError, OSError) as exc:
                rest = unfinished_delete(planned, deleted)
                if rest:
                    return KeptDeleteRetry(KEPT_STARTED, str(exc), tuple(deleted), tuple(rest))
                return KeptDeleteRetry(KEPT_KEPT, str(exc), tuple(deleted))
            if removal.kept_reason:
                return KeptDeleteRetry(KEPT_KEPT, removal.kept_reason)
            return KeptDeleteRetry(KEPT_DELETED, None, tuple(deleted))
        finally:
            await self._let_go_in_background(self._delete_holds.pop(owner_id, []))

    def _finish_problem(
        self, source_path: str, output_path: str, meta: dict, recorded_source: str,
        remaining: List[str], owner_id: str, jobs: List[ConversionJob],
    ) -> Optional[str]:
        """Why a part-deleted Source's remaining files must stay, or None.
        Blocking; run under the locks (:meth:`finish_kept_delete`)."""
        if os.path.realpath(source_path) != recorded_source:
            return "the output's check record names another Source"
        if self._output_fingerprint(output_path) != meta.get("output"):
            return "the output is gone or changed since its check"
        if self._companion_fingerprints(
            output_path, str(meta.get("mode") or ""),
        ) != (meta.get("companions") or {}):
            return "a companion output is gone or changed since its check"
        # Each remaining file is one the delete planned and the check
        # fingerprinted, unchanged, and still safe to remove.
        by_real = {
            os.path.realpath(path): fp for path, fp in (meta.get("source") or {}).items()
        }
        fingerprints = {}
        for path in remaining:
            fp = by_real.get(os.path.realpath(path))
            if fp is None:
                return "a remaining file is not one the check fingerprinted"
            fingerprints[path] = fp
        return self._delete_path_problem(
            owner_id, remaining, fingerprints, os.path.realpath(output_path),
            set(remaining), jobs,
        )

    async def _report_unfinished_delete(
        self, job: ConversionJob, rest: List[str], exc: BaseException,
    ) -> None:
        """A job's delete step stopped part way (#329). A Rule's job hands the
        rest to its sweep (``job._unfinished_delete``, finished like a kept
        Source); a hand-started job has no pending store or Library, so a
        ``source_kept`` Finding in the ``findings.NO_LIBRARY`` bucket names the
        part-deleted Source at once. Its cause lists the files left, so the
        Findings view clears it once they are gone."""
        job._unfinished_delete = list(rest)
        if job._sweep_owner is not None:
            return
        from services import findings

        try:
            await run_detached_bounded(
                findings.observe, findings.NO_LIBRARY, findings.started_delete(
                    job.file_path, source=job.file_path, output=job.output_path,
                    why=f"its delete failed ({exc})", left=list(rest),
                ),
                timeout=KEPT_DELETE_PROBE_SECONDS,
            )
        except (DeadlineExpired, OSError):
            logger.warning(
                "Could not record the part-deleted Source of %s", job.file_path,
                exc_info=True,
            )

    async def finish_kept_delete(
        self, *, source_path: str, output_path: str, planned: List[str],
        required_check: Optional[str] = None,
    ) -> KeptDeleteRetry:
        """Finish a retried delete that outlived its wait.

        The Source is part deleted by design, so it is not planned again.
        Under the output's, its companions' and the remaining files' locks,
        this re-checks that the output and its companions are still exactly
        the checked ones, re-runs the output's check, and that every remaining
        file is one the check fingerprinted, unchanged; then removes them in
        order, as one mutation. ``deleted`` when nothing of the plan is left;
        ``stale`` when the output no longer stands (the remaining files stay,
        and the caller reports the part-deleted Source); ``reader``, ``kept``
        or ``unknown`` as for :meth:`retry_kept_delete`.
        """
        async with self.kept_delete(
            source_path=source_path, output_path=output_path,
            required_check=required_check, planned=planned,
        ) as run:
            return run.refused or await run.delete()

    async def _prepare_finish(
        self, run: "KeptDeleteRun", owner_id: str, travelling: HeldLocks,
        source_path: str, output_path: str, planned: List[str],
        required_check: Optional[str], stopping: Callable[[], bool],
    ) -> Optional[KeptDeleteRetry]:
        """:meth:`kept_delete` for a Source whose delete started. The
        Round-trip cannot run against a part-deleted Source, so the output's
        container check is re-run, and every remaining file must still be the
        one the check fingerprinted."""
        refused = await self._take_output(output_path, travelling)
        if refused is not None:
            return refused
        found = await self._kept_record(output_path, required_check)
        if isinstance(found, KeptDeleteRetry):
            return found
        _check, meta, record = found
        try:
            remaining = await run_detached_bounded(
                lambda: [path for path in planned if os.path.lexists(path)],
                timeout=KEPT_DELETE_PROBE_SECONDS,
            )
            taken = await run_detached_undoable(
                self._take_exclusively,
                [*(meta.get("companions") or {}), *remaining],
                timeout=KEPT_DELETE_PROBE_SECONDS, undo=_let_go,
            )
        except DeadlineExpired:
            return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
        except (LockFailed, OSError) as exc:
            return KeptDeleteRetry(KEPT_KEPT, str(exc))
        if taken is None:
            return KeptDeleteRetry(KEPT_READER, SOURCE_KEPT_FOR_READERS)
        for path in taken:
            travelling.add(path)

        async def problem() -> Optional[KeptDeleteRetry]:
            try:
                found_problem = await run_detached_bounded(
                    self._finish_problem, source_path, output_path, meta,
                    record["source_path"], remaining, owner_id,
                    list(self.jobs.values()),  # a snapshot, taken on the loop
                    timeout=KEPT_DELETE_PROBE_SECONDS,
                )
            except DeadlineExpired:
                return KeptDeleteRetry(KEPT_KEPT, KEPT_DELETE_UNANSWERED)
            if found_problem:
                return KeptDeleteRetry(KEPT_STALE, found_problem, (), tuple(remaining))
            return None

        refused = await problem()
        if refused is not None:
            return refused
        if stopping():
            return KeptDeleteRetry(KEPT_STOPPED, "stopped before the output's check")
        refused = await self._recheck_output(None, output_path, meta, required_check)
        if refused is not None:
            if refused.status == KEPT_STALE:
                # The files left stay, and are named in the caller's Finding.
                return KeptDeleteRetry(KEPT_STALE, refused.reason, (), tuple(remaining))
            return refused

        async def delete() -> KeptDeleteRetry:
            # The cheap filter once more, right before the unlinks.
            refused_now = await problem()
            if refused_now is not None:
                return refused_now
            if stopping():
                return KeptDeleteRetry(KEPT_STOPPED, "stopped before the unlinks")
            removed: List[str] = []
            try:
                await run_detached_mutation(
                    _remove_in_order, remaining, removed,
                    timeout=KEPT_DELETE_PROBE_SECONDS,
                    hand_off=lambda: self._hand_off_locks(owner_id, travelling),
                )
            except MutationInFlight as exc:
                rest = unfinished_delete(remaining, removed, in_flight=True)
                return KeptDeleteRetry(
                    KEPT_UNKNOWN, str(exc), tuple(removed), tuple(rest or remaining),
                )
            except asyncio.CancelledError:
                # Cancelled mid-plan: the delete has
                # started either way, so what is left (or still running on
                # the worker) stays for the next sweep; the caller records
                # that, then this goes on.
                rest = unfinished_delete(remaining, removed, in_flight=True)
                run.started = KeptDeleteRetry(
                    KEPT_UNKNOWN, "the delete was cancelled", tuple(removed),
                    tuple(rest or remaining),
                )
                raise
            except (MutationNotStarted, OSError) as exc:
                rest = unfinished_delete(remaining, removed)
                if rest:
                    return KeptDeleteRetry(KEPT_STARTED, str(exc), tuple(removed), tuple(rest))
                return KeptDeleteRetry(KEPT_KEPT, str(exc), tuple(removed))
            await self._clear_source_records(source_path, KEPT_DELETE_PROBE_SECONDS)
            return KeptDeleteRetry(KEPT_DELETED, None, tuple(removed))

        run.set_delete(delete)
        return None

    async def _delete_check(
        self, job, tool, source_path: str, output_path: str, cancel_event,
    ) -> _DeleteCheck:
        """Which check the output passed, and whether the Source may go.

        Called once the container check has passed. The mode's delete proof
        (``ModeSpec.delete_proof``) decides what runs next, through the shared
        ``round_trip_applies`` / ``recognized_applies``, so no caller branches
        on the tool:

        * Round-trip: ``intact`` when the container check is all this job asks
          for, ``round_tripped`` when the output's content digest equalled the
          Source's, and ``intact`` with a ``fail_reason`` when it ran and did
          not pass.
        * Recognized (#423): ``recognized`` with the DAT that knows the
          output's SHA-1, or ``intact`` with a ``keep_reason`` on a miss.

        Either runs only when it decides something: the Source is to be
        deleted. A job that keeps its Source reads nothing extra.
        """
        mode = job.mode.value
        if recognized_applies(mode, delete_on_verify=job.delete_on_verify):
            return await self._recognized_check(job, tool, output_path, cancel_event)
        if not round_trip_applies(
            mode,
            delete_on_verify=job.delete_on_verify,
            container_only=job.container_only,
        ):
            return _DeleteCheck("intact")
        # The container check has passed. Record that now, so a Cancel during
        # the comparison (which raises before this returns) still leaves the
        # job and its Ledger row saying Intact; only a match upgrades it.
        await self._say_intact(job, "Comparing its content with the Source...")
        # Reported as verifying, not stalled: it reads whole files and emits
        # no progress, exactly like the container check before it.
        self._verifying[job.id] = time.monotonic()
        try:
            # An archive-member or bound Source (and any sheet it names)
            # lives in the job's own folder outside the Volumes; the tool may
            # read it there. Anything else stays confined to the Volumes.
            outcome = await round_trip(
                tool, source_path, output_path, cancel_event=cancel_event,
                within=_trusted_roots(job),
            )
        finally:
            self._verifying.pop(job.id, None)
        if outcome.cancelled:
            raise ConversionCancelled("Conversion cancelled")
        if outcome.passed:
            return _DeleteCheck("round_tripped")
        return _DeleteCheck("intact", fail_reason=outcome.message)

    async def _recognized_check(
        self, job, tool, output_path: str, cancel_event,
    ) -> _DeleteCheck:
        """The Recognized proof of a job's output (#423), under the tool's
        verify bound. A miss is a ``keep_reason``: the output is fine, nothing
        vouches for it, so the Source stays and the job completes."""
        await self._say_intact(job, "Looking its SHA-1 up in the DATs...")
        self._verifying[job.id] = time.monotonic()
        try:
            bound = await tool.verify_timeout(output_path, cancel_event=cancel_event)
            outcome = await recognize(
                output_path, cancel_event=cancel_event, timeout=bound,
            )
        finally:
            self._verifying.pop(job.id, None)
        if outcome.cancelled:
            raise ConversionCancelled("Conversion cancelled")
        if outcome.passed:
            return _DeleteCheck(RECOGNIZED, dat_name=outcome.dat_name)
        return _DeleteCheck("intact", keep_reason=outcome.message)

    async def _say_intact(self, job, then: str) -> None:
        """Record and announce that the container check passed, before a
        delete proof that reads the whole file runs (*then* says which)."""
        job.check_passed = "intact"
        job.message = f"Output is Intact. {then}"
        await self._notify_subscribers(
            job.id,
            {
                "type": "progress",
                "job_id": job.id,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def _container_check(
        self, tool, output_path: str, mode: str, cancel_event: asyncio.Event,
        *, verifying_id: str | None = None,
    ) -> str:
        """The output's container check: the tool's ``verify`` of what it says
        to verify (``verify_target``), under the tool's own verify bound
        (``verify_timeout``). Returns the path it verified.

        The one check for every caller: a job after its conversion, and a
        retried delete before it removes anything (#329).
        Raises :class:`VerifyUnanswered` when nothing was judged (the bound
        ran out, the storage stopped answering), ``ConversionCancelled`` on a
        Cancel, and ``RuntimeError`` when the output fails its check, after
        recording that failure in the verification store (``mark_failed``,
        #351), so a later re-check that finds a once-checked output bad no
        longer leaves it shown as checked.
        *verifying_id* is the job reported as verifying (not stalled) meanwhile.
        """
        verify_path = await self._verify_target_bounded(
            tool, output_path, mode, cancel_event,
        )
        if not verify_path:
            raise RuntimeError("Verification could not find the converted output")
        verify_bound = await tool.verify_timeout(verify_path, cancel_event=cancel_event)
        if verifying_id is not None:
            self._verifying[verifying_id] = time.monotonic()
        try:
            verify_result = await asyncio.wait_for(
                tool.verify(verify_path, cancel_event=cancel_event),
                timeout=verify_bound or None,
            )
        except asyncio.TimeoutError:
            raise VerifyUnanswered(f"Verification timed out after {verify_bound}s") from None
        finally:
            if verifying_id is not None:
                self._verifying.pop(verifying_id, None)
        if verify_result.get("cancelled"):
            # The verifier stopped because Cancel was pressed, so it reached
            # no verdict. Reporting that as a failed verification would be
            # wrong twice over: it is not a bad file, and the source must not
            # be deleted on it.
            raise ConversionCancelled("Conversion cancelled")
        if verify_result.get("abandoned"):
            # The verifier outlived SIGKILL and is still holding the storage.
            # Distinguished from an ordinary failure so the log and the error
            # name the real problem -- the file was never judged, the mount
            # stopped answering -- rather than reading as "this output is bad".
            logger.error(
                "Verification could not be stopped and is still holding %s; "
                "the volume is likely not responding", output_path,
            )
            raise VerifyUnanswered(
                f"Verification could not be stopped: {verify_result.get('message')}"
            )
        verdict = check_verdict(verify_result)
        if verdict is None and not verify_result.get("uncheckable"):
            # The verifier reached no verdict (its own bound ran out, the file
            # could not be read): the same as the outer bound running out.
            # A job fails on it as before; a retried
            # delete counts an attempt and tries again, never "stale". An
            # uncheckable file stays a failed check: retrying cannot help.
            raise VerifyUnanswered(
                f"Verification reached no verdict: {verify_result.get('message')}"
            )
        if not verify_result.get("valid"):
            # A real verdict that the output is bad is recorded the way every
            # re-check records one (`mark_failed`, `check_verdict`), so no
            # badge, `/api/verified` lookup or delete warning keeps presenting
            # it as checked, for every caller of this check (#351). No verdict
            # (a timeout, a Cancel, an abandoned or uncheckable verify) records
            # nothing.
            if verdict is False:
                await verification_store.mark_failed(output_path)
            raise RuntimeError(f"Verification failed: {verify_result.get('message')}")
        return verify_path

    async def _verify_target_bounded(
        self, tool, output_path: str, mode: str, cancel_event,
    ) -> str | None:
        """What to verify, resolved without letting a dead mount keep the job.

        Ask the tool rather than assuming the planned path holds the artifact:
        a split makeps3iso build writes `<iso>.0`/`.1`/… and no bare `.iso`, so
        verifying `output_path` read a file that was never created and failed a
        conversion that had in fact succeeded.

        Bounded and detached because of *when* this runs. It sits between the
        conversion finishing and `verify_timeout()` being resolved, so it is
        ahead of every bound the verify itself carries — and the answer is a
        stat of the output plus a scan for numbered parts, on storage that has
        just taken a multi-gigabyte write and is therefore the likeliest thing
        in the process to have stopped answering. Blocked here the job holds
        its output lock and its source-directory lock, keeps `_verifying`
        empty so the watchdog sees nothing to report, and with
        MAX_CONCURRENT_JOBS at its default of 1 freezes every job behind it.

        The cancel event is honoured for the same reason it is honoured by the
        verify: a Cancel pressed while a mount is not answering must return
        now, not after the probe bound expires. The probe thread is abandoned,
        never the pool's — `run_detached` exists for exactly this.
        """
        try:
            return await asyncio.wait_for(
                run_detached(
                    tool.verify_target, output_path, mode, cancel_event=cancel_event,
                ),
                timeout=_CANONICAL_PROBE_SECONDS,
            )
        except ReadCancelled:
            # Cancel, not a verdict. Same translation the verify itself makes:
            # nothing was judged, and the source must not be deleted on it.
            raise ConversionCancelled("Conversion cancelled") from None
        except asyncio.TimeoutError:
            raise VerifyUnanswered(
                "Verification could not determine what to check "
                f"in {_CANONICAL_PROBE_SECONDS:.0f}s "
                "(the storage holding the output stopped responding)"
            ) from None

    async def _source_bytes_bounded(
        self, job: ConversionJob, cancel_event: Optional[asyncio.Event],
    ) -> Optional[int]:
        """Size the job's Source set for the Ledger, without risking the queue.

        Optional bookkeeping, so a timeout leaves the size unknown rather than
        failing the job; a Cancel returns now and is a cancellation. Same
        detached, bounded probe as `_verify_target_bounded`, for the same
        reason: the job already holds its slot and locks.
        """
        try:
            return await asyncio.wait_for(
                run_detached(
                    _source_bytes, _reads(job), cancel_event=cancel_event,
                ),
                timeout=_CANONICAL_PROBE_SECONDS,
            )
        except ReadCancelled:
            raise ConversionCancelled("Conversion cancelled") from None
        except asyncio.TimeoutError:
            logger.warning(
                "Sizing the source of job %s did not finish in %.0fs; "
                "the Ledger row will have no input size",
                job.id, _CANONICAL_PROBE_SECONDS,
            )
            return None

    async def _detect_platform_bounded(
        self, job: ConversionJob, cancel_event: Optional[asyncio.Event],
    ) -> Optional[str]:
        """The Platform shared detection gives the job's Source, for the
        Ledger (#346), or None when it can't place it.

        The detection Auto uses (``platform_detect.detect_platform``, with its
        cache), through the shared bounded probe (``run_detached_bounded``)
        under the same bound as the Source sizing: optional bookkeeping, so a
        timeout or an unreadable Source leaves it unknown, and a Cancel
        returns now as a cancellation.
        """
        from services.platform_detect import detect_platform

        try:
            detection = await run_detached_bounded(
                detect_platform, _reads(job),
                timeout=_CANONICAL_PROBE_SECONDS, cancel_event=cancel_event,
            )
        except ReadCancelled:
            raise ConversionCancelled("Conversion cancelled") from None
        except (DeadlineExpired, ProbeCapacityExceeded, OSError):
            logger.warning(
                "Detecting the Platform of job %s did not finish; "
                "the Ledger row will have none", job.id, exc_info=True,
            )
            return None
        return detection.platform

    async def _process_job(self, job_id: str):
        """Process a single conversion job."""
        job = self.jobs.get(job_id)
        if not job:
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Starting job %s status=%s input=%s output=%s",
                job_id,
                job.status.value,
                job.file_path,
                job.output_path,
            )

        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            self._cancelled.discard(job_id)
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            cancel_event.set()
            self._cancelled.discard(job_id)
            if not job.completed_at:
                job.completed_at = datetime.now(timezone.utc)
            # Both early exits below return before the try/finally that
            # notifies for every other outcome, so they announce themselves.
            # Listeners may hear about one job twice (this path runs after
            # `cancel_job` already fired) and must be idempotent.
            self._notify_terminal(job)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
            await self._cleanup_temp_dir(job)
            del self._cancel_events[job_id]
            await self._prune_jobs(exclude_id=job_id)
            return

        # If this job's path is inside a folder another job is currently packing
        # (makeps3iso folder->iso), don't fail — wait in the queue and retry once
        # that folder job releases its subtree lock, like any queued job.
        if await run_in_threadpool(self._blocked_by_dir_lock, job):
            await self._defer_blocked_job(job_id, output_lock_held=False)
            return

        # Try to acquire lock for the output file (prevents race conditions)
        lock_error = None
        try:
            lock_acquired = await run_in_threadpool(self._take_output_locks, job)
        except LockFailed as exc:
            # Not contention: the lock file can't be made. Fail with the
            # reason now, never requeue.
            lock_acquired, lock_error = False, str(exc)
        if not lock_acquired:
            # A transient block by a folder->iso subtree lock (racing the
            # precheck) waits & re-queues rather than failing.
            if lock_error is None and await run_in_threadpool(self._blocked_by_dir_lock, job):
                await self._defer_blocked_job(job_id, output_lock_held=False)
                return
            # Could not acquire lock - either file exists or is being converted
            # Check current status to provide better error message
            file_exists, is_locked = lock_manager.check_file_status(job.output_path)
            job.status = JobStatus.FAILED
            if lock_error is not None:
                job.error_message = lock_error
            elif is_locked:
                job.error_message = (
                    "Another job is already converting to this output file"
                )
            elif file_exists:
                job.error_message = "Output file already exists"
            else:
                job.error_message = "Could not acquire lock for output file"
            job.completed_at = datetime.now(timezone.utc)

            # Terminal, and it returns below without reaching the try/finally
            # that announces every other outcome -- so a listener recording how
            # this job ended would never hear about the one case where the job
            # failed *before* touching the output.
            self._notify_terminal(job)
            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": job.error_message}
            )
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        # acquire_lock above already rejects a destination that exists but isn't a
        # plain file — incl. a *directory* shadowing the output path — via its
        # ``not os.path.isfile`` clause (for both overwrite states). What it can't
        # see is a prior split set (``Game.iso.0``/``.1`` with no bare
        # ``Game.iso``): os.path.exists(bare) is False, so the lock is granted. A
        # non-overwrite job would then clobber that existing deliverable and, on a
        # later failure, unlink its parts via remove_outputs. Mirror the bare-file
        # "already exists" rejection for the split set. (An authorized overwrite
        # clears it in _clear_existing_output.)
        if job.input_kind == InputKind.DIRECTORY and not job.allow_overwrite:
            existing_parts = await run_in_threadpool(
                registry.for_mode(job.mode.value).companion_outputs,
                job.output_path, job.mode.value,
            )
            # companion_outputs returns the numbered split parts only when a real
            # -s split set exists (a bare .iso is the primary, not a companion,
            # and is already rejected by acquire_lock above), so any companions
            # mean a prior split build already occupies the target.
            if existing_parts:
                job.status = JobStatus.FAILED
                job.error_message = "Output file already exists"
                job.completed_at = datetime.now(timezone.utc)
                # Same as the lock failure above: an early return, so it has to
                # announce itself.
                self._notify_terminal(job)
                await self._notify_subscribers(
                    job_id,
                    {"type": "error", "job_id": job_id, "error": job.error_message},
                )
                self._release_output_locks(job)
                if job_id in self._cancel_events:
                    del self._cancel_events[job_id]
                await self._cleanup_temp_dir(job)
                await self._prune_jobs(exclude_id=job_id)
                return

        # A directory-input job (makeps3iso folder->iso) additionally locks its
        # whole source subtree, so any concurrent per-file job / rename / delete
        # whose path falls inside the folder contends on the lock instead of
        # corrupting the in-flight ISO. A conflict here means another job is
        # already operating inside the folder; fail like an output collision.
        dir_lock_acquired = False
        if job.input_kind == InputKind.DIRECTORY:
            try:
                dir_lock_acquired = await run_in_threadpool(
                    lock_manager.acquire_dir_lock, job.file_path,
                )
            except LockFailed as exc:
                await self._fail_before_start(job_id, job, str(exc), dir_lock_held=False)
                return
            if not dir_lock_acquired:
                # Another job is operating inside this folder; wait in the queue
                # and retry rather than failing (releases the output lock taken
                # just above).
                await self._defer_blocked_job(job_id, output_lock_held=True)
                return

        # Every job, whichever entry point queued it, holds a shared lock on
        # the files it reads until it ends (#306). Other processes see it: the
        # web guard's exact-path probe refuses to rename or delete them, and a
        # job writing one of those paths can't take its output lock. Readers
        # share it. A folder input already holds its subtree lock above.
        # From the moment the input locks may be taken until the try/finally
        # below owns them: a cancellation here (worker shutdown) releases
        # them, or the Source would stay shared-locked until restart (#306).
        # The hidden staging slot the output is written into until its checks
        # pass, when the output's Library needs one (#398); closed in the
        # `finally`, which discards whatever was never placed.
        slot: staging.StagedOutput | None = None
        # The staged output, when a Referrer places it itself (#391).
        placement: referrers.StagedPlacement | None = None
        # Set once the output is to be placed (its checks passed, or none was
        # asked): from then on a staged output that is not placed is kept,
        # never discarded.
        placeable = False
        try:
            try:
                inputs_held =job.input_kind == InputKind.DIRECTORY or await run_in_threadpool(
                    self._hold_inputs, job_id, job, self._begin_input_attempt(job_id),
                )
            except Exception as exc:
                # Never stay queued holding a slot: any failure ends the job.
                logger.warning("Could not lock the Source of %s", job_id, exc_info=True)
                await self._fail_before_start(
                    job_id, job, f"Could not lock the Source: {exc}",
                    dir_lock_held=dir_lock_acquired,
                )
                return
            if not inputs_held:
                await self._defer_blocked_job(
                    job_id,
                    output_lock_held=True,
                    message="Waiting for another job using this Source or its folder...",
                )
                return

            job.status = JobStatus.PROCESSING
            job.started_at = datetime.now(timezone.utc)
            # Restart the progress clock at the moment work actually begins. It was
            # last set when the job was created, so a job that waited in the queue
            # longer than STALL_WARNING_SECONDS -- routine on a deep queue -- would
            # otherwise be reported stalled the instant it started running.
            self._last_progress_at[job_id] = time.monotonic()
            # The jobs holding a conversion slot, by the dispatcher's own rule:
            # a check run or scan beside this job is PROCESSING but holds none;
            # an Inbox copy holding one (`hold_slot`, #431) counts.
            processing_now = self.slots_in_use()
            if processing_now > self.max_concurrent:
                logger.error(
                    "Processing concurrency invariant violated: processing=%d max_concurrent=%d",
                    processing_now,
                    self.max_concurrent,
                )

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Job %s acquired locks and started processing", job_id)

            await self._notify_subscribers(
                job_id,
                {
                    "type": "status",
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": 0,
                },
            )
        except BaseException:
            self._release_inputs(job_id)
            raise

        try:
            # The Source is still the file the job was queued for, read beneath
            # the folder it must stay under with no link followed (#431), now
            # that its lock is held and before anything reads it. Inside this
            # region: a cancel while either waits lets
            # go of the output locks and the slot too. A refusal ends the job
            # FAILED with nothing written or removed: no output exists yet.
            problem = await self._source_identity_problem_bounded(job, cancel_event)
            if problem is None:
                problem = await self._bind_source_bounded(job, cancel_event)
            if problem is not None:
                if cancel_event.is_set():
                    raise ConversionCancelled("Conversion cancelled")
                raise SourceRefused(f"Not converted: {problem}")
            # Ledger bookkeeping (#292): size the Source set before anything can
            # delete it. Inside the cleanup-protected region, on a detached
            # thread, bounded and cancellable like the verify-target probe: a
            # mount that stops answering must not strand this job's slot and
            # locks. Optional data, so a timeout just leaves it unknown.
            if job.input_bytes is None:
                job.input_bytes = await self._source_bytes_bounded(job, cancel_event)
            # And the Platform the Ledger files it under (#346), for a job no
            # Library placed (hand-started: file browser, Auto, the CLI).
            if job.platform is None:
                job.platform = await self._detect_platform_bounded(job, cancel_event)

            # Fast path: a re-queued job whose verified artifact already exists
            # completes as a no-op rather than re-spawning the converter (issue
            # #184, site 1). Checked before extraction / _clear_existing_output
            # so the existing verified output is neither re-extracted-from nor
            # deleted. Honors a cancel requested before we got here.
            if await self._complete_from_verified_output(job, cancel_event):
                return

            # A bound Source (#431) is read from its folder in the work area.
            input_path = _reads(job)
            # The trusted root(s) a source's track sheet may name files under
            # (utils.sheet_text.resolve_track's ``within``): None for a source
            # in a Volume; the folder it is read from for one outside them,
            # a bound Source's or, set below, an archive
            # member's extraction dir. Built once here (`_trusted_roots`) and
            # handed to every consumer.
            within = _trusted_roots(job)
            if "::" in job.file_path:
                input_path = await self._extract_archive_member(job, cancel_event)
                within = _trusted_roots(job)

            # Same point-in-time problem as the directory check below, for a
            # multi-file *file* source: a queued split Wii U set's
            # `game_partN.wud` can be swapped for a symlink out of the volumes
            # after `plan_job` cleared it, and the converter enumerates and
            # opens the parts itself. Re-check under the job's locks, and
            # before clearing any existing output so a rejection on an
            # overwrite job leaves the user's prior file intact.
            if not await run_in_threadpool(
                source_companions_are_safe, input_path, job.mode.value,
                within=within,
            ):
                await self._fail_now(job, (
                    "A companion file this source consumes is a symlink or "
                    "resolves outside configured volumes"
                ))
                return

            if job.input_kind == InputKind.DIRECTORY:
                # Re-check directory-input safety after acquiring the source
                # subtree lock and immediately before invoking the native
                # recursive packer. A queued PS3 folder job may have been
                # valid when planned but mutated before it starts; this guards
                # the exact tree makeps3iso is about to read. Run it *before*
                # clearing any existing output so a safety rejection on an
                # overwrite job is non-destructive: the user's prior ISO/split
                # set is only removed once the source is confirmed still safe.
                if not await run_in_threadpool(is_safe_directory_tree, input_path):
                    await self._fail_now(job, (
                        "PS3 folder contains symlinks, special files, "
                        "or paths outside configured volumes"
                    ))
                    return

            # The recursive PS3 safety walk above can take a while on a large
            # source tree; honor a cancellation requested during it before
            # deleting the user's existing output, mirroring the post-extract
            # cancel check. Otherwise an overwrite job cancelled mid-walk would
            # still clear the prior ISO/split set only to abort immediately.
            if cancel_event.is_set():
                raise ConversionCancelled("Conversion cancelled")

            await self._clear_existing_output(job)

            # Where the tool writes: the output path itself, or the same name
            # in a hidden staging slot when the output's Library has its own
            # scanner (#398). Every step up to the placing below works on it;
            # job.output_path stays the final path throughout.
            slot = await self._open_stage(job)
            write_path = (
                slot.path_for(os.path.basename(job.output_path))
                if slot is not None else job.output_path
            )

            _convert_service = registry.for_mode(job.mode.value)
            async for update in _convert_service.run_convert(
                input_path,
                write_path,
                job.mode.value,
                compression=job.compression,
                split=job.split,
                cancel_event=cancel_event,
            ):
                if cancel_event.is_set():
                    continue
                now = time.monotonic()
                # The runner tells us which updates were real forward movement
                # (a percentage that advanced, or the output file growing) and
                # which were keep-alives. Don't re-derive it: counting every
                # arrival makes a heartbeating-but-hung job look alive, and
                # counting only percentage changes makes a healthy job look
                # stalled once the size fallback pins at 95% or when its mode
                # has no size ratio at all (issue #263).
                if update.get("activity"):
                    self._last_progress_at[job_id] = now
                job.progress = update["progress"]
                _set_status_message(job, update["message"])
                # A tool's caveat (an eXoDOS conf that points outside its game,
                # a chain step's warning) is kept on the job: later messages,
                # such as the verify step's, replace job.message.
                if update.get("warning"):
                    job.warning = job.message
                if logger.isEnabledFor(logging.DEBUG):
                    last_log = self._last_progress_log_at.get(job_id, 0)
                    if now - last_log >= PROGRESS_LOG_SECONDS:
                        self._last_progress_log_at[job_id] = now
                        logger.debug(
                            "Job %s progress=%s message=%s",
                            job_id,
                            job.progress,
                            job.message,
                        )

                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "progress",
                        "job_id": job_id,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )

            if job.status != JobStatus.CANCELLED:
                self._cancelled.discard(job_id)
                job.progress = 100

                # Run the tool's post-convert hook before sizing the output.
                # chdman uses it to record a freshly created CD/DVD CHD's disc
                # ID (read from the Source) in the metadata store; the default
                # is a no-op, so this is safe to call for every mode. The source
                # (input_path) is still present here, even when delete-on-verify
                # is requested (deletion happens after verify).
                try:
                    await registry.for_mode(job.mode.value).post_convert(
                        input_path, write_path, job.mode.value, within=within,
                    )
                except DiscIdStorageAbandoned:
                    # Deliberately not swallowed with the rest. Everything below
                    # touches job.output_path on the same storage -- the output
                    # size probe, and delete-on-verify spawns a verifier -- so
                    # let the outer handler fail this job now rather than spend
                    # each later bound on storage known to be gone (#263/#268).
                    raise
                except Exception as e:
                    logger.debug(
                        "Job %s post-convert hook skipped: %s", job_id, e
                    )

                # Output size = primary + companions (extractcd's .bin, a split
                # folder_to_iso's numbered parts), via the shared companion-aware
                # helper. Off the event loop and bounded: a split folder_to_iso's
                # companion lookup scans the output dir, and an output Volume
                # that stops answering must fail the job, not hold its slot.
                job.output_size = await self._output_size_bounded(job, write_path)

                verified = False
                source_deleted = False
                # Two ways to ask for the same verify: delete-on-verify needs it
                # as a precondition, verify_after wants the check on its own and
                # keeps the source. One block runs it either way, so a verified
                # output means the same thing however it was requested.
                if job.delete_on_verify or job.verify_after:
                    tool, verify_path = await self._verify_output(
                        job, write_path, cancel_event,
                    )
                    verified = True
                    decided = await self._decide_delete(
                        job, tool, input_path, write_path, verify_path, cancel_event,
                    )
                    # Both checks passed: only now does a staged output
                    # appear beside the Source (#398). Everything below
                    # works on the placed output, unless a Referrer places
                    # it itself (the RomM swap, #391): it then stays staged
                    # until the Referrer hook, and its records are written
                    # where it is staged and carried along when it moves.
                    # Not from a bound Source that changed meanwhile.
                    await self._require_bound_unchanged(job)
                    placeable = True
                    placement = await self._staged_placement(job, slot, write_path)
                    if placement is None:
                        await self._place_staged(job, slot, write_path)
                    checked_path = write_path if placement is not None else job.output_path
                    check_label = await self._record_check(
                        job, checked_path, decided.check,
                    )
                    source_deleted = await self._finish_delete_on_verify(
                        job, decided, check_label, placement, cancel_event,
                    )

                if slot is not None and not slot.placed:
                    # Not from a bound Source that changed meanwhile.
                    await self._require_bound_unchanged(job)
                placeable = True
                if (
                    slot is not None and not slot.placed
                    and not (placement is not None and placement.held.is_set())
                ):
                    # No check was asked for: the output is placed once the
                    # conversion has finished (#398). Never when a Referrer
                    # that stopped part way holds it (#391).
                    await self._place_staged(job, slot, write_path)

                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "complete",
                        "job_id": job_id,
                        "output_path": job.output_path,
                        "output_size": job.output_size,
                        "verified": verified,
                        "check_passed": job.check_passed,
                        "source_deleted": source_deleted,
                    },
                )

        except ConversionCancelled:
            self._cancelled.discard(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
        except asyncio.CancelledError:
            # The runner's task was cancelled (a shutdown), not the job's Cancel
            # button: the job still ends CANCELLED, so the terminal listeners
            # (`finally`) hear it and nothing is left PROCESSING.
            # Then the cancel goes on.
            self._cancelled.discard(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(  # queue puts only: never suspends
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
            raise
        except Exception as e:
            self._cancelled.discard(job_id)
            job.status = JobStatus.FAILED
            # Whatever tool output the error carries, without its control
            # characters (#455): the one sink every conversion failure meets.
            job.error_message = clean_tool_output(str(e))
            # The status line says why it stopped, not its last progress (#466).
            job.message = job.error_message or "Failed"
            job.completed_at = datetime.now(timezone.utc)

            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": job.error_message}
            )

        finally:
            # Whatever the set-aside below raises, a cancel or any error, is
            # held until every release after it has run, then raised.
            set_aside_cancel: BaseException | None = None
            if placeable and slot is not None and not slot.placed:
                # A checked output that could not be placed (its name was
                # taken, a Referrer holds it) is kept, out of every sweep,
                # with its records, and the job says where, before the Ledger
                # hears of it. Its slot is never closed below, cancelled or
                # not: a set-aside the cancel cut short finishes on its worker.
                aside, slot = slot, None
                try:
                    await self._set_aside_output(job, aside)
                except BaseException as exc:
                    # Raised again once every release below has run.
                    logger.warning(
                        "Job %s: setting its output aside ended early", job.id, exc_info=True,
                    )
                    set_aside_cancel = exc
            # Before any cleanup: this is the one place every runner outcome
            # (completed, failed, cancelled mid-run) passes through, and a
            # listener that must record how the job ended has to hear about it
            # while the job object still says so.
            self._notify_terminal(job)
            # Only release lock if we acquired it. The output locks (unless an
            # unlink took them) and the delete holds, in the background like
            # a retry's: lock-file I/O never on the loop.
            # Each release in its own `finally`: a cancel
            # while one awaits (the output locks' bounded wait) must not skip
            # the input and folder locks, or the concurrency slot, after it.
            # The cancel goes on once all of them ran.
            held_outputs = self._output_locks.pop(job.id, None) if lock_acquired else None
            try:
                await self._let_go_in_background([
                    *(held_outputs.take() if held_outputs is not None else []),
                    *self._delete_holds.pop(job_id, []),
                ])
            finally:
                try:
                    # Release the directory subtree lock held by a folder->iso job.
                    if dir_lock_acquired:
                        lock_manager.release_dir_lock(job.file_path)
                finally:
                    try:
                        self._release_inputs(job_id)
                    finally:
                        self._forget_job_runtime(job_id)
                try:
                    try:
                        # Whatever was never placed goes, with the slot (#398).
                        await self._close_stage(job, slot)
                    finally:
                        # Clean up temp directory if this was an archive extraction
                        await self._cleanup_temp_dir(job)
                finally:
                    await self._prune_jobs(exclude_id=job_id)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Job %s finished status=%s", job_id, job.status.value)
            if set_aside_cancel is not None:
                raise set_aside_cancel

    async def _complete_from_verified_output(
        self, job: ConversionJob, cancel_event: asyncio.Event,
    ) -> bool:
        """Complete *job* at once when its verified output already exists."""
        if cancel_event.is_set() or not await self._output_already_verified(job):
            return False
        # Re-check after the awaited store/stat lookups: a cancel that
        # landed while they were in flight must win over the no-op.
        if cancel_event.is_set():
            raise ConversionCancelled("Conversion cancelled")
        self._cancelled.discard(job.id)
        job.progress = 100
        job.output_size = await self._output_size_bounded(job)
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        # No check ran in this job, so job.check_passed stays unset;
        # the event reports the check the reused output already
        # passed, so the UI doesn't downgrade a Round-tripped badge.
        stored_check = await self._stored_check(job.output_path)
        job.message = (
            f"Output already {_CHECK_LABELS[stored_check]}; "
            "skipped re-conversion."
        )
        await self._notify_subscribers(
            job.id,
            {
                "type": "complete",
                "job_id": job.id,
                "output_path": job.output_path,
                "output_size": job.output_size,
                "verified": True,
                "check_passed": stored_check,
                "source_deleted": False,
            },
        )
        return True

    async def _extract_archive_member(
        self, job: ConversionJob, cancel_event: asyncio.Event,
    ) -> str:
        """Extract the archive member *job* reads into its temp dir; its path."""
        extract_start = time.monotonic()
        archive_path, internal_path = job.file_path.split("::", 1)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Job %s extracting archive %s member %s",
                job.id,
                archive_path,
                internal_path,
            )
        extract_task = asyncio.create_task(
            run_in_threadpool(
                archive_service.extract_file, archive_path, internal_path
            )
        )
        extract_status_task = asyncio.create_task(
            self._emit_extract_updates(
                job.id, archive_path, internal_path, extract_task
            )
        )
        input_path, temp_dir = await extract_task
        extract_status_task.cancel()
        try:
            await extract_status_task
        except asyncio.CancelledError:
            pass
        job.temp_dir = temp_dir
        await run_in_threadpool(
            archive_service.extract_related_files,
            archive_path,
            internal_path,
            temp_dir,
        )
        if logger.isEnabledFor(logging.DEBUG):
            extracted_size = None
            try:
                extracted_size = os.path.getsize(input_path)
            except OSError:
                pass
            logger.debug(
                "Job %s extracted to %s size=%s in %.2fs",
                job.id,
                input_path,
                extracted_size,
                time.monotonic() - extract_start,
            )
        if cancel_event.is_set():
            raise ConversionCancelled("Conversion cancelled")
        return input_path

    async def _notify_progress(self, job: ConversionJob) -> None:
        await self._notify_subscribers(job.id, {
            "type": "progress", "job_id": job.id,
            "progress": job.progress, "message": job.message,
        })

    async def _fail_now(self, job: ConversionJob, message: str) -> None:
        """End *job* FAILED with *message*, before anything was written."""
        job.status = JobStatus.FAILED
        job.error_message = message
        job.completed_at = datetime.now(timezone.utc)
        await self._notify_subscribers(
            job.id, {"type": "error", "job_id": job.id, "error": job.error_message},
        )

    async def _verify_output(
        self, job: ConversionJob, write_path: str, cancel_event: asyncio.Event,
    ) -> tuple:
        """Run *job*'s requested check on its output: ``(tool, verify_path)``."""
        # Both halves of the contract, re-asked here because this is
        # where the source actually gets unlinked. The plan sites
        # (`routes/convert.py`, `romm_auto.normalize_rule`) check the
        # same pair, but a job can reach this point without passing
        # either -- restored queue state, a hand-edited rule blob, a
        # future caller. `supports_delete_on_verify` says the mode
        # can offer it at all; `delete_on_verify_is_safe` says THIS
        # job can, given its compression, and that is the one that
        # stops a Wii U `noverify` conversion from deleting a 25 GB
        # source on a structural check.
        if job.delete_on_verify and not (
            registry.spec(job.mode.value).supports_delete_on_verify
            and registry.for_mode(job.mode.value).delete_on_verify_is_safe(
                job.mode.value, job.compression,
            )
        ):
            raise RuntimeError(
                "Delete-on-verify is not safe for this conversion: "
                "the mode or its compression settings cannot prove "
                "the output before the source is removed"
            )
        if cancel_event.is_set():
            raise ConversionCancelled("Conversion cancelled")

        job.message = "Verifying output..."
        await self._notify_progress(job)

        tool = registry.for_mode(job.mode.value)
        # Bound the whole verify, not just whatever subprocess it
        # happens to spawn: a tool whose verify is pure Python (the
        # Wii U container walk, the PS3 PARAM.SFO readback) has no
        # subprocess timeout to hide behind, and with
        # MAX_CONCURRENT_JOBS=1 a verify that never returns freezes
        # every job queued behind it (issue #266). The tool resolves
        # the number so its own per-tool override applies, and takes
        # the cancel event because sizing the output is a stat of
        # its own: on a mount that has stopped answering, a Cancel
        # pressed here must not wait out the probe bound before the
        # verify that would report it even starts.
        # Ask the tool what to verify rather than assuming the
        # planned path holds it: a split makeps3iso build writes
        # `<iso>.0`/`.1`/… and no bare `.iso`, so verifying
        # `output_path` read a file that was never created and
        # failed a conversion that had in fact succeeded.
        # The shared check (`_container_check`), the one a retried
        # delete re-runs before it removes anything.
        return tool, await self._container_check(
            tool, write_path, job.mode.value, cancel_event,
            verifying_id=job.id,
        )

    async def _decide_delete(
        self, job: ConversionJob, tool, input_path: str, write_path: str,
        verify_path: str, cancel_event: asyncio.Event,
    ) -> "_DeleteCheck":
        """Which check the output passed, and whether it may delete the Source."""
        # The delete decision (#298): which check the output
        # passed, and, when it is not enough to delete the Source,
        # why. The job (and so the Ledger row) records the check
        # either way.
        decided = await self._delete_check(
            job, tool, input_path, verify_path, cancel_event,
        )
        job.check_passed = decided.check
        job.check_dat = decided.dat_name
        if decided.fail_reason:
            # The Round-trip did not pass, so this output may not
            # hold the Source's content. Store nothing that would
            # present it as a checked result: no badge, no reuse
            # snapshot for the re-run fast path. A failed check
            # keeps provenance: the record names this output's
            # mode and tool (replacing whatever an earlier run
            # left) and is marked failed, so the next check still
            # goes to the tool that made it. A staged output is
            # never placed: its records go with it (#398).
            await verification_store.mark_verified(
                write_path,
                source_path=job.file_path,
                produced_meta=self._verifier_identity(job),
            )
            await verification_store.mark_failed(write_path)
            raise RuntimeError(decided.fail_reason)
        return decided

    async def _record_check(
        self, job: ConversionJob, checked_path: str, check: str,
    ) -> str:
        """Store the passed check for *checked_path*; the check's label."""
        # Snapshot what produced this verified artifact (mode +
        # output shaping + source/output fingerprints) so a later
        # re-queue can recognize prior success and complete as a
        # no-op without re-spawning the converter (#184, site 1).
        # The source still exists here (delete runs below), so its
        # complete set can be fingerprinted.
        # Bounded like the output size (#352): it stats the
        # output and the Source on the same storage.
        snapshot_meta = await run_detached_within_stall_bound(
            self._build_produced_meta, job, checked_path,
            what=f"Fingerprinting {os.path.basename(job.output_path)}",
        )
        # The verifier identity (the mode and tool that made the
        # output) is recorded for every passing check, with or
        # without a delete snapshot: the CLI routes the next check
        # by it (#306). The fingerprints that back the re-run fast
        # path come only from the snapshot.
        produced_meta = {
            **self._verifier_identity(job), **(snapshot_meta or {}),
        }
        await verification_store.mark_verified(
            checked_path,
            source_path=job.file_path,
            produced_meta=produced_meta,
            check=check,
        )
        return _CHECK_LABELS[check]

    async def _finish_delete_on_verify(
        self, job: ConversionJob, decided: "_DeleteCheck", check_label: str,
        placement, cancel_event: asyncio.Event,
    ) -> bool:
        """Keep or delete the Source once the output passed; True if deleted."""
        deleted = False
        if not job.delete_on_verify or decided.keep_reason:
            # verify_after only: the source is kept by design, so
            # the job is finished the moment the output checks out.
            # Or the output is Intact but its delete proof found
            # nothing to vouch for it (not Recognized, #423): the
            # Source stays and the job completes, saying why.
            job.message = f"Output is {check_label}."
            if decided.keep_reason:
                job.warning = decided.keep_reason
                job.message = f"Output is {check_label}, {decided.keep_reason}"
            await self._notify_progress(job)
        elif cancel_event.is_set():
            job.message = (
                f"Output is {check_label}. Delete skipped (cancelled)."
            )
            await self._notify_progress(job)
        elif not await self._hold_delete_paths_waiting(job.id, cancel_event):
            if cancel_event.is_set() or job.id in self._cancelled:
                # Cancelled while it waited: the Source stays and
                # the job ends CANCELLED, never reported as a
                # reader.
                raise ConversionCancelled("Conversion cancelled")
            # Another job is still reading the Source (or a folder
            # above it): the output is done and checked, so the job
            # completes, keeping the Source.
            job.warning = SOURCE_KEPT_FOR_READERS
            job.message = f"Output is {check_label}. {SOURCE_KEPT_FOR_READERS}."
            await self._notify_progress(job)
        else:
            delete_label = (
                "source archive"
                if "::" in job.file_path
                else "source"
            )
            job.message = (
                f"Output is {check_label}. Deleting {delete_label}..."
            )
            await self._notify_progress(job)

            snapshot = self._delete_plans.get(job.id)
            if not snapshot or not snapshot.get("paths"):
                raise RuntimeError(
                    "Delete plan snapshot missing; refusing to delete"
                )

            # Every delete path is held exclusively from here until
            # the unlinks finish (``_hold_delete_paths_waiting``,
            # above): no job can start reading one between the
            # checks and its unlink (#306).
            job_planned: List[str] = []
            job_handed_off: List[bool] = []
            try:
                removal = await self._delete_source_set(
                    job.id,
                    file_path=job.file_path,
                    output_path=job.output_path,
                    expected_paths=list(snapshot.get("paths", [])),
                    fingerprints=snapshot.get("fingerprints") or {},
                    check_label=check_label,
                    cancel_event=cancel_event,
                    cancelled=lambda: job.id in self._cancelled,
                    deleted=job.deleted_paths,
                    # The output locks travel with an unlink this
                    # job stops waiting for (a cancel), like a
                    # retry's.
                    travelling_locks=self._output_locks.get(job.id),
                    planned=job_planned,
                    handed_off=job_handed_off,
                    placement=placement,
                    hook_records=job.referrers_updated,
                )
            except asyncio.CancelledError:
                # The task itself was cancelled once the unlinks
                # began, or while the first still ran: what started, or is still running
                # on the worker, is recorded (bounded, never
                # waiting on the unlink worker), the job ends
                # CANCELLED and is announced (the `finally`
                # below), then the cancel goes on.
                rest = unfinished_delete(
                    job_planned, job.deleted_paths,
                    in_flight=bool(job_handed_off),
                )
                if rest:
                    await self._report_unfinished_delete(
                        job, rest, "the job was cancelled",
                    )
                raise
            except (RuntimeError, OSError) as exc:
                rest = unfinished_delete(
                    job_planned, job.deleted_paths,
                    in_flight=bool(job_handed_off),
                )
                if rest is None:
                    raise
                # Started, then refused part way: never silent.
                await self._report_unfinished_delete(job, rest, exc)
                raise RuntimeError(
                    f"Source part deleted: {len(job.deleted_paths)} of "
                    f"{len(job_planned)} files removed, {len(rest)} remain "
                    f"({exc})",
                ) from exc
            job.referrers_updated = removal.referrer_records
            if removal.cancelled:
                # One last look before anything is unlinked
                # (#306): a cancel here ends CANCELLED.
                raise ConversionCancelled("Conversion cancelled")
            if removal.kept_reason:
                # The Source stays and the job completes: say why
                # where every reader of a finished job looks (the
                # CLI's report, the job row): job.warning.
                job.message = removal.kept_reason
                job.warning = removal.kept_reason
                await self._notify_progress(job)
            else:
                deleted = True
                if removal.notes:
                    # Said, never left to look like a swap (#391).
                    job.warning = "; ".join(removal.notes)
                    job.message = f"Output is {check_label}. {job.warning}"
        return deleted

    def _forget_job_runtime(self, job_id: str) -> None:
        """Drop a finished job's in-memory runtime bookkeeping. Never raises."""
        self._delete_plans.pop(job_id, None)
        self._output_keys.pop(job_id, None)
        self._cancel_events.pop(job_id, None)
        self._last_progress_at.pop(job_id, None)
        self._last_progress_log_at.pop(job_id, None)
        self._last_stall_log_at.pop(job_id, None)
        self._last_output_size.pop(job_id, None)
        self._last_output_size_at.pop(job_id, None)


job_manager = JobManager(
    max_concurrent=settings.max_concurrent_jobs,
    max_job_history=settings.max_job_history,
)
