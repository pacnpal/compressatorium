"""The sweep: one pass of a Library's Rules, queueing what is due.

Each Rule names a Platform of a Library (see :mod:`services.rules.library`).
A sweep asks the Library for that Platform's Images, filters and orders them,
and queues a conversion for each one whose target output is missing.

Three properties this is built for, in order:

**Idempotent.** An Image is queued only when its target output is genuinely
absent, and that answer comes from ``ToolPlugin.detect_output()`` -- the same
registry-driven detector that badges rows in the file browser. The filesystem
is the state, so a sweep that runs twice, runs after a restart, or races a
manual conversion converges instead of duplicating. Only the ``overwrite``
and ``rename`` policies need memory beyond that (the converted history).

**Deterministic.** Platforms run in priority order and Images in the Rule's
chosen order, so a capped sweep resumes predictably rather than sampling the
Library at random.

**Bounded.** Every sweep is capped per Platform and overall, respects the
queue's own backpressure, and stops rather than spins -- because the failure
mode of the alternative is discovering a 2,000-Image Platform and committing
the operator's disk and CPU for a week on the strength of one checkbox.

Anything only one kind of Library does (RomM's metadata snapshot, its
identity-cleanup gate, its re-pin backlog) happens in that Library's hooks.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from models import ConversionMode, JobStatus
from services import findings, notifications, subprocess_runner
from services.image_sets import image_fingerprint
from services.job_manager import (
    KEPT_DELETED,
    KEPT_KEPT,
    KEPT_STALE,
    KEPT_STARTED,
    KEPT_STOPPED,
    KEPT_UNKNOWN,
    SOURCE_KEPT_FOR_READERS,
    KeptDeleteRetry,
    OutputClaimedError,
    QueueBackpressureError,
    job_manager,
)
from services.output_conflicts import (
    QUEUE,
    SKIP_EXISTING,
    SKIP_LOCKED,
    is_renamed_from,
    resolve_destination,
)
from services.rules import check as checks
from services.rules import store
from services.rules.library import (
    CONVERTED_HISTORY,
    FAILED_HOLD,
    Image,
    Library,
    LibraryUnreadable,
    QueuedImage,
    duplicate_actions,
    refuse_unsupported_policies,
)
from services.rules.rules import (
    CHECK,
    CONVERT,
    RULE_KINDS,
    OUTPUT_IDENTITY_FIELDS,
    _output_identity,
    _validation_paused,
    effective_check_level,
    normalize_rules,
    takes_container_only,
)
from services.rules.schedule import _is_due
from services.subprocess_runner import bounded_path_check, run_detached
from services.tools import InputKind, registry
from utils.delete_plan import build_delete_snapshot
from utils.path_utils import (
    is_within_configured_volumes,
    match_extension,
    path_fingerprint,
    source_companions_are_safe,
)

# The logger name the engine has always logged under (see `rules.logger`).
logger = get_logger("romm_auto")

# How long one candidate's disk probes may take before the library counts as
# unresponsive. Generous for a healthy mount (these are a resolve and two
# stats) and short enough that a dead one costs a sweep rather than a restart.
_CANDIDATE_PROBE_SECONDS = 30

# Sources a Rule's delete step kept because another job was reading them
# (#329), by Platform in the Rule state: ``{image key: {"source", "output",
# "job_id", "kept_at", "attempts", ...}}``. Each later sweep retries the
# delete step for them (``job_manager.retry_kept_delete``); one a reader holds
# again is retried up to KEPT_DELETE_MAX_ATTEMPTS times, then dropped with a
# ``source_kept`` Finding. At most KEPT_DELETES_PER_SWEEP are tried a sweep.
KEPT_SOURCES = "kept_sources"
KEPT_DELETE_MAX_ATTEMPTS = 10
KEPT_DELETES_PER_SWEEP = 25
# How long recording a delete a cancel stopped may take: the cancel waits on it.
_CANCEL_RECORD_SECONDS = 10
# How long the save-time output-directory validation may take. Generous: it
# resolves each candidate and stats every configured volume, and a healthy
# local mount answers instantly. See `normalize_rules_bounded`.
_VOLUME_PROBE_SECONDS = 20
# How long listing one Platform's Images may take (a Library's catalog call, or
# a folder Library's walk). Generous, since a cold walk of a large folder over
# SMB reads a header per file; bounded, because the sweep holds `_sweep_lock`
# for the whole call and a mount that stops answering would otherwise hold it
# until a restart.
_LISTING_SECONDS = 600
# How often a long wait in the sweep (a Library listing) looks for a stop.
_STOP_POLL = 0.2


class _SweepStopped(Exception):
    """The caller's ``should_stop`` turned true during a wait."""


async def _wait_unless_stopped(awaitable, timeout: float, stopping: Callable[[], bool]):
    """Await *awaitable* for up to *timeout* seconds, giving up on a stop.

    Raises ``asyncio.TimeoutError`` past the bound and :class:`_SweepStopped`
    once *stopping()* is true. Either way the awaited task is cancelled; for a
    ``run_detached`` call that abandons the worker thread, which stays bounded
    and finishes on its own.
    """
    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            done, _ = await asyncio.wait({task}, timeout=min(_STOP_POLL, remaining))
            if done:
                return task.result()
            if stopping():
                raise _SweepStopped
    finally:
        if not task.done():
            task.cancel()

# One sweep at a time, process-wide. The active-source snapshot is taken before
# anything is queued, so the minute scheduler and a manual "Run now" could each
# read the same "nothing in flight" and queue the same ROM twice -- the job
# manager's creation lock serialises the inserts but does not deduplicate
# across two independently planned batches.
_sweep_lock = asyncio.Lock()

# job id -> (library, platform_id, image key, the queuing Rule's output
# identity, the Source's image_fingerprint when queued) for the conversions
# this process queued. Read by `note_job_finished` so a completion updates its
# one record instead of scanning every remembered Image of every Platform. The
# fingerprint (None unless the Library holds failures) binds a failure hold to
# the Source version the job converted. In memory on purpose: the job queue it
# indexes is in memory too, so an entry has nothing to outlive.
_job_owners: dict[str, tuple[Library, str, str, tuple | None, str | None]] = {}

# library id -> its generation, bumped when the Library is removed. A job-finish
# callback captures it before its first await and writes state only if it is
# unchanged, checked under `_sweep_lock`: a Library removed while the callback
# was probing must not have its state written back.
_generations: dict[str, int] = {}

# (library id, platform id) -> the reason it last could not be listed. A
# Platform that stays unreadable (a folder that is gone) is retried every tick;
# this logs the change of state, once each way, instead of every tick.
_unreadable: dict[tuple[str, str], str] = {}

# (library id, platform id) of each check Rule whose scheduled run is still
# checking (#305). Its run is recorded only when that run finishes, so until
# then the Rule would read as due and the next tick would pick another budget's
# worth; while it is here the scheduler picks nothing more for it. Cleared as
# the run ends, recorded or not. In memory: one scheduler runs per data
# directory (#307's scheduler lock), and a restart ends every run.
_active_check_runs: set[tuple[str, str]] = set()

# job id -> outcome, for a job that ended before its record was written. Drained
# by `_mark_converted`; see there. Bounded by the same thing that bounds
# `_job_owners`: one entry per queued conversion, removed when it is applied.
_late_verdicts: dict[str, bool] = {}
# What each owned job's delete was decided under, stamped when it was queued:
# ``(_delete_decision_identity, decision generation)``. A job that ends after
# either moved records no pending delete (#329).
_job_decision: dict[str, tuple[dict, int]] = {}


async def get_rules(library: Library, kind: str = CONVERT) -> dict[str, dict]:
    """*library*'s stored Rules of *kind*, normalized without touching the filesystem.

    `check_volumes=False`: the output directory was validated when it was
    saved, and re-validating on every read would put a resolve plus a stat of
    every configured volume on the event loop -- in the rules API and in each
    sweep, which holds `_sweep_lock` while it runs. The sweep re-checks the
    destination off the loop before it queues anything, which is what covers a
    blob edited straight in the database.
    """
    stored = await store.read(store.rules_key(kind), library)
    return _normalize(library, stored, kind=kind, check_volumes=False)


def _normalize(library: Library, raw: Any, *, kind: str = CONVERT, **kwargs) -> dict[str, dict]:
    """`normalize_rules` with *library*'s Platform ids and defaults."""
    return normalize_rules(
        raw,
        kind=kind,
        platform_id=library.canonical_platform_id,
        defaults=library.rule_defaults(),
        duplicate_actions=duplicate_actions(library),
        **kwargs,
    )


@asynccontextmanager
async def paused():
    """Hold off the sweep for the duration of the block.

    Anything that changes what a sweep *means* -- which RomM it talks to,
    where the library is mounted, what each rule targets -- has to take this,
    not just the bookkeeping that follows it. A sweep holds the ROM records it
    fetched but resolves each one's local path lazily, so swapping the library
    root underneath a running sweep makes it queue conversions for whatever
    unrelated files happen to sit at the same relative paths in the new
    library. With delete-on-verify on, it then deletes them.
    """
    async with _sweep_lock:
        yield


async def _probe(func, *args, default):
    """One blocking filesystem answer, bounded, with the safe answer on timeout.

    Every stat and resolve in the sweep goes through here rather than
    `run_in_threadpool`: the pool is shared and has no deadline, and the sweep
    holds `_sweep_lock` throughout -- so one unresponsive mount would block
    previews, manual runs, rule edits and settings saves behind it, having
    stranded a pooled worker on the way.

    *default* is what "the volume did not answer" means at that call site, and
    it is always the conservative reading.
    """
    try:
        return await bounded_path_check(func, *args)
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm_auto: %s did not answer in time; treating it as %r "
            "(a volume is not answering)",
            getattr(func, "__name__", func), default,
        )
        return default


async def _ready_bounded(tool) -> bool:
    """Is this tool usable here, without risking the sweep on a dead volume?

    Delegates to :meth:`ToolRegistry.tool_is_ready`, which is where the bound
    and the reasoning live now that the two routes that fan out over the whole
    registry need the same thing. Kept as a name here because the *consequence*
    is local: the sweep holds `_sweep_lock` throughout, so an unresponsive
    mount would block previews, manual runs, rule edits and settings saves
    behind it — and not-ready skips the platform with a reason the editor
    shows.
    """
    return await registry.tool_is_ready(tool)


async def _within_volumes_bounded(path: str) -> bool:
    """Is *path* inside the configured volumes, without risking the sweep?

    The same stat-every-volume check as at save time, and the sweep holds
    `_sweep_lock` while it runs — so a saved output directory that has since
    become an unresponsive mount would block previews, manual runs, rule edits
    and settings saves behind it, having stranded a pooled worker on the way.

    A bound that expires answers *no*: the platform is skipped with a reason
    the editor shows, which is what an unwritable destination deserves.
    """
    try:
        return bool(await asyncio.wait_for(
            run_detached(is_within_configured_volumes, path),
            _VOLUME_PROBE_SECONDS,
        ))
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm_auto: checking output_dir %r did not finish in %ss; skipping "
            "the platform (a volume is not answering)", path, _VOLUME_PROBE_SECONDS,
        )
        return False


async def normalize_rules_bounded(
    library: Library, raw: Any, kind: str = CONVERT,
) -> dict[str, dict]:
    """`normalize_rules` with its volume checks off the event loop, bounded.

    Validating an output directory resolves the candidate and stats every
    configured volume. `set_rules` runs inside `_sweep_lock`, so on an
    unresponsive NFS/SMB/rclone mount that check freezes the whole API *and*
    everything waiting on the lock -- including the request to disable the very
    rule that names the bad path.

    A bound that expires means the volumes could not be read, which pauses
    every rule with an output directory rather than accepting one nobody could
    check. Disruptive, and correct: those conversions had nowhere to write.
    """
    try:
        return await asyncio.wait_for(
            run_detached(functools.partial(_normalize, library, raw, kind=kind)),
            _VOLUME_PROBE_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm_auto: validating the rules' output directories did not finish "
            "in %ss; pausing the rules that name one (a volume is not answering)",
            _VOLUME_PROBE_SECONDS,
        )
        return _normalize(library, raw, kind=kind, volumes_unreachable=True)


async def set_check_rules(library: Library, raw: Any) -> dict[str, dict]:
    """Replace *library*'s check Rules (#305). Its conversion Rules are untouched.

    Under the sweep lock, as :func:`set_rules`. A check Rule writes nothing,
    so it has no output folder to validate and no converted history to forget.
    """
    async with _sweep_lock:
        rules = _normalize(library, raw, kind=CHECK, check_volumes=False)
        await store.write(store.CHECK_RULES_KEY, library, rules)
        return rules


async def set_rules(library: Library, raw: Any, *, strict: bool = False) -> dict[str, dict]:
    """Replace *library*'s conversion rule set, forgetting provenance the new rules invalidate.

    *strict* is for a submission: a Rule asking for a duplicate policy the
    Library cannot honour raises :class:`UnsupportedPolicy` and nothing is
    saved. Without it the policy falls back to ``skip``, as a stored blob does.

    Switching a platform from RVZ to GCZ, or pointing it at a new output
    directory, asks for a different file than the one already produced. Left
    alone, ``converted_ids`` would report every source as done and the
    retargeted rule would convert nothing, with no way to say otherwise --
    so a rule that changes what it produces starts its history over.
    """
    # Under the sweep lock, and for the whole read-compare-write. A sweep
    # holds the rules it started with, and finishes by writing back the ROM ids
    # it queued and its schedule stamp under the same platform key. Clearing
    # the history alongside it would let the old sweep's write land after the
    # clear, so the retargeted rule would skip ROMs it never produced its new
    # format for -- and inherit the old run's clock as well.
    if strict:
        refuse_unsupported_policies(library, raw)
    async with _sweep_lock:
        previous = await get_rules(library)
        # Off the loop and bounded: this stats every configured volume, and the
        # lock we are holding is the one a rescue edit would need. See above.
        rules = await normalize_rules_bounded(library, raw)
        stale = [
            pid for pid in previous
            if _output_identity(previous[pid]) != _output_identity(rules.get(pid))
        ]
        # History first, rules second. They are two writes and either can be
        # the one an interruption spares, so the order decides which failure
        # you get: clearing history for a retarget that then fails to save
        # costs a re-conversion, while saving a retarget whose history survives
        # makes `overwrite`/`rename` skip every ROM as already converted --
        # against outputs that belong to the *former* target, and with no way
        # to notice, because a retry compares the new rule with itself and
        # computes no stale platform at all. Recoverable beats silent.
        if stale:
            # Its pending deletes too: they were decided under the former output.
            await forget_converted_locked(library, stale)
        # A change to anything else the delete is decided on (deleting at all,
        # the check level) invalidates the pending deletes, not the history.
        redecided = [
            pid for pid in previous if pid not in stale
            and _delete_decision_identity(previous[pid], library)
            != _delete_decision_identity(rules.get(pid), library)
        ]
        if redecided:
            state = await get_state(library)
            await _invalidate_decisions(library, state, redecided)
            await store.write(store.STATE_KEY, library, state)
        # Any change at all moves the Platform's Rule revision on: a pending
        # delete being tried right now is re-planned, never deleted, under a
        # Rule it was not planned for (codex 4580441297). Only a Platform with
        # pending deletes can have one being tried, so no other state moves.
        revised = sorted(
            pid for pid in {*previous, *rules} if previous.get(pid) != rules.get(pid)
        )
        if revised:
            state = await get_state(library)
            pending = [
                pid for pid in revised
                if isinstance(state.get(pid), dict) and state[pid].get(KEPT_SOURCES)
            ]
            for pid in pending:
                entry = dict(state[pid])
                entry[RULE_REVISION] = _rule_revision(entry) + 1
                state[pid] = entry
            if pending:
                await store.write(store.STATE_KEY, library, state)
        await store.write(store.RULES_KEY, library, rules)
        return rules


async def forget_converted(
    library: Library, platform_ids: list[str] | None = None,
) -> int:
    """Drop *library*'s converted-id history for *platform_ids* (or all of them).

    The operator-facing escape hatch: restoring ROMs from a backup, or moving
    outputs out of the way by hand, leaves the library in a state only they
    can see. Returns how many platforms were cleared.
    """
    async with _sweep_lock:
        return await forget_converted_locked(library, platform_ids)


async def forget_converted_locked(
    library: Library, platform_ids: list[str] | None = None,
    *, owner: str | None = None,
) -> int:
    """:func:`forget_converted` for a caller already inside :func:`paused`.

    The one point that invalidates what a Rule decided for its Images: the
    converted history, the failed holds, and the Sources kept for a reader
    that are waiting to be deleted (#329). Every invalidation goes through
    here (or :func:`_invalidate_decisions`, which this calls): Forget history,
    a Rule's retarget or delete change (``set_rules``), RomM's identity
    cleanup, a Library's removal (``forget_library``). *owner* is where a
    started delete's Finding goes when the Library will not hold it
    (``findings.NO_LIBRARY``: RomM's identity cleanup drops its own).
    """
    state = await get_state(library)
    targets = (
        [str(p) for p in platform_ids] if platform_ids is not None
        else sorted({*state, *(await get_rules(library))})
    )
    cleared = 0
    for pid in targets:
        entry = state.get(pid)
        if isinstance(entry, dict) and (
            entry.get("converted") or entry.get("converted_ids") or entry.get("failed")
        ):
            entry = dict(entry)
            entry.pop("converted", None)
            entry.pop("converted_ids", None)
            # The failed hold too: "forget" is how an operator retries an
            # Image without changing its file.
            entry.pop("failed", None)
            state[pid] = entry
            cleared += 1
    # Every decision made for these Platforms is invalid now, pending deletes
    # and jobs still running alike (the generation moves on).
    if await _invalidate_decisions(library, state, targets, owner=owner) or cleared:
        await store.write(store.STATE_KEY, library, state)
    return cleared


def library_generation(library_id: str) -> int:
    """*library_id*'s generation: it moves when that Library is removed.

    Anything that writes on a Library's behalf from outside the sweep lock
    (the Findings a folder walk records) captures it first and writes only
    while it is unchanged. Safe from any thread: a plain dictionary read.
    """
    return _generations.get(library_id, 0)


async def forget_library(library: Library) -> None:
    """Drop every Rule and all run state *library* has. Touches no files.

    For a Library being removed. Under the sweep lock, so a sweep of it that is
    running finishes first and cannot write its state back afterwards, and the
    owner index loses the Library's queued jobs so their outcomes are not
    written back either.
    """
    async with _sweep_lock:
        # A started delete is reported before its entry goes, under no
        # Library: this one's Findings are dropped with it.
        state = await get_state(library)
        await _invalidate_decisions(library, state, list(state), owner=findings.NO_LIBRARY)
        _generations[library.id] = _generations.get(library.id, 0) + 1
        for job_id, owner in list(_job_owners.items()):
            if owner[0].id == library.id:
                _job_owners.pop(job_id, None)
                _job_decision.pop(job_id, None)
                await _orphan_job(library, owner[2], job_manager.jobs.get(job_id))
        for key in [k for k in _unreadable if k[0] == library.id]:
            _unreadable.pop(key, None)
        for kind in RULE_KINDS:
            await store.write(store.rules_key(kind), library, {})
            await store.write(store.state_key(kind), library, {})


async def _orphan_job(library: Library, image_key: str, job) -> None:
    """A job of a Library being removed has no sweep owner any more (codex
    4580441301): it reports its own partial delete from now on, under no
    Library, and one it already handed over is reported here, since its
    owner will not take it."""
    if job is None:
        return
    job._sweep_owner = None
    unfinished = getattr(job, "_unfinished_delete", None)
    if unfinished:
        await _report_unfinished(
            library, image_key, job, list(unfinished), owner=findings.NO_LIBRARY,
        )


async def get_state(library: Library, kind: str = CONVERT) -> dict[str, dict]:
    """*library*'s run state for its Rules of *kind*: schedule stamps and, for
    conversions, converted history, by Platform."""
    return await store.read(store.state_key(kind), library)


async def _record_run(
    library: Library, platform_id: str, summary: dict, kind: str = CONVERT,
    *, manual: bool = False, at: datetime | None = None, listed_at: int,
) -> bool:
    """Stamp the schedule clock, merging into whatever else the entry holds.

    The per-platform entry is shared with ``converted_ids``: replacing it
    wholesale would forget, on every single run, which sources the rule had
    already converted -- and that set is the only thing stopping an
    ``overwrite`` rule from reconverting the library forever.

    *listed_at* is the Library's walk generation when the run listed its
    Images. A run is recorded only if that listing is still current now, at
    the moment it would be recorded: for a check Rule that is when its check
    job ends, so conversions that land while it checks leave it due too
    (#353, #360). Otherwise nothing is written, *summary* gets
    ``stale_listing`` and this returns False; the next tick lists again. Every
    path that records a run comes through here.
    """
    if not _listing_current(library, listed_at):
        summary["stale_listing"] = True
        logger.info(
            "romm_auto: %s's %s run for platform %s saw a listing a finished job "
            "made out of date; not recorded, so the next tick runs it again",
            library.id, kind, platform_id,
        )
        return False
    state = await get_state(library, kind)
    entry = dict(state.get(str(platform_id)) or {})
    # A manual run of a check Rule (Run now) stamps its own field: `_is_due`
    # reads only `last_run_at`, so the nightly run is not pushed back a day.
    clock = "last_manual_run_at" if manual else "last_run_at"
    # *at* is the moment the sweep found the Rule due, so a pick that runs past
    # midnight still counts for the window that selected it.
    stamp = at or datetime.now(timezone.utc)
    entry.update({
        clock: stamp.isoformat().replace("+00:00", "Z"),
        "last_queued": summary.get("queued", 0),
        "last_considered": summary.get("considered", 0),
    })
    if summary.get("check"):
        entry["last_checks"] = summary.get("checks", 0)
    state[str(platform_id)] = entry
    await store.write(store.state_key(kind), library, state)
    return True


# ----------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------


def _key_order(image: Image):
    """An Image's key as a tie-breaker. A missing key sorts as 0."""
    return image.key if image.key is not None else 0


def _image_sort_key(rule: dict) -> Callable[[Image], tuple]:
    order = rule["order"]
    if order == "size_desc":
        return lambda i: (-(i.size or 0), _key_order(i))
    if order == "size_asc":
        return lambda i: ((i.size or 0), _key_order(i))
    if order == "id":
        return lambda i: (_key_order(i),)
    if order == "newest":
        return lambda i: (-(i.added_at or 0),)
    return lambda i: ((i.title or "").lower(), _key_order(i))


def _passes_filters(image: Image, rule: dict) -> bool:
    name = image.name or ""
    size = image.size or 0
    if rule["min_size_mb"] and size < rule["min_size_mb"] * 1024 * 1024:
        return False
    if rule["max_size_mb"] and size > rule["max_size_mb"] * 1024 * 1024:
        return False
    if rule["include_pattern"] and not re.search(rule["include_pattern"], name, re.IGNORECASE):
        return False
    if rule["exclude_pattern"] and re.search(rule["exclude_pattern"], name, re.IGNORECASE):
        return False
    # The Library's own identification (`Image.recognized`; for RomM, its
    # "identified" status, always True or False). Unknown (None) passes
    # neither filter: a Library that cannot say, because nothing matched the
    # Image yet or its match cache could not be read, must not have an
    # only_unmatched Rule convert everything (fail closed).
    if rule["only_matched"] and image.recognized is not True:
        return False
    return not (rule["only_unmatched"] and image.recognized is not False)


def _resolve_source(path: str) -> str | None:
    """Absolute, symlink-free source path, or None when it cannot be resolved.

    Its own function so the sweep can push it into a worker thread: resolving a
    path stats every component, and doing that per ROM on the event loop stalls
    the whole app for the length of a catalog.
    """
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return None


def _compression_arg(rule: dict) -> str | None:
    """The rule's compression as the job pipeline expects it: ``"codec:level"``.

    The level is not a separate parameter anywhere downstream -- ``/api/jobs``
    encodes it into the compression string and the tools parse it back out --
    so sending the codec alone silently dropped the level the operator set. A
    level with the codec on "tool default" is ``":level"``; the tool names its
    own codec for it when the job runs (``ToolPlugin.compression_for``, #345),
    as it does for a job from anywhere else.
    """
    codec = rule["compression"]
    level = rule["compression_level"]
    if level is None:
        return codec or None
    return f"{codec or ''}:{level}"


def _accepts_source(tool, spec, path: str) -> bool:
    """Whether this *mode* takes *path* as an input unit.

    The mode, not the tool. `converts_path` answers a tool-wide,
    direction-agnostic question, and for a tool whose directions have
    different inputs it answers backwards: chdman deliberately drops `.chd`
    from its tool-level extensions so a finished CHD is not badged as a
    convertible source, yet `.chd` is exactly what its extract and copy modes
    take. Gating the sweep on it rejected every real `copy` source and
    accepted `.iso` files that mode cannot consume.

    Directory modes (makeps3iso's decrypted PS3 folder) declare no input
    extensions at all, so the extension match rejects every one of them; the
    registry says which predicate applies.

    Where the tool *does* claim the extension, its own predicate still gets
    the final word: that is where the per-file refinements live (jwud leaves
    the secondary members of a split Wii U dump visible but non-convertible,
    since only `game_part1.wud` drives the set).
    """
    if registry.mode_input_kind(spec.mode) is InputKind.DIRECTORY:
        return tool.accepts_directory(path)
    if spec.input_extensions and match_extension(path, spec.input_extensions) is None:
        return False
    if match_extension(path, tool.input_extensions) is not None:
        return tool.converts_path(path)
    return bool(spec.input_extensions)


async def _converted_map(library: Library, platform_id: str) -> dict:
    """What this rule has produced, as ``{image_key: {"path", "pre"}}``.

    Provenance the filesystem cannot supply on its own. ``skip`` is idempotent
    from the destination alone, but the other two policies are not:

    * ``overwrite`` resolves an existing destination as queueable by
      definition, so a standing rule reconverts and rewrites the same
      multi-gigabyte image every interval, forever;
    * ``rename`` moves to the next free suffix each time, so the same source
      accumulates ``Game_1``, ``Game_2``, ... until the volume fills.

    The record is *not* "this was queued" -- queueing is not producing, and a
    job that is cancelled, or interrupted by a restart, or fails in the
    converter would otherwise mark its ROM done forever. What is stored is the
    destination and a fingerprint of whatever occupied it at planning time, so
    "did this actually happen" is answered by the destination having changed
    since. A conversion that never ran leaves it untouched and the next sweep
    picks the ROM up again, with no bookkeeping to keep in sync and nothing to
    reconcile after a crash.

    Bounded by the platform's ROM count, which is the same order as the catalog
    each sweep already holds in memory.
    """
    state = await get_state(library)
    entry = state.get(str(platform_id), {})
    stored = entry.get("converted")
    out = {str(k): v for k, v in stored.items() if isinstance(v, dict)} if isinstance(
        stored, dict,
    ) else {}
    # An earlier shape recorded ids alone, with no evidence of production.
    # Honour them as done rather than reconverting a library on upgrade.
    for rom_id in entry.get("converted_ids") or []:
        out.setdefault(str(rom_id), {"path": "", "pre": None})
    return out


def _was_produced(remembered: dict | None) -> bool:
    """Whether the conversion recorded in *remembered* actually happened.

    The job's own outcome first, and the filesystem only as a fallback. A
    changed destination is not proof of success: a failed or cancelled
    ``overwrite`` job can unlink the previous artifact or leave a partial one
    behind, which looks exactly like a fresh output from the outside -- and
    the ROM would then be skipped by every later sweep until the operator
    cleared the history by hand.

    The job id is the precise answer while the queue still remembers it, which
    covers the window that matters: the next sweep is minutes away. Job history
    is pruned and does not survive a restart, so after that the fingerprint is
    the best evidence left -- weaker, but it only ever mis-reads a *destroyed*
    destination, and `Forget history` is the way back from that.

    ``pre`` is None for a legacy record that carries no evidence either way;
    those are trusted, since the alternative is reconverting a whole library.
    """
    if not remembered:
        return False
    # The verdict the queue already gave us, written down when the job ended
    # (`note_job_finished`). It is the only evidence that survives a prune or a
    # restart, so it is asked first and nothing below can override it.
    if "done" in remembered:
        return bool(remembered["done"])
    job_id = remembered.get("job_id")
    if job_id:
        job = job_manager.get_job(job_id)
        if job is not None:
            if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
                return True  # in flight; not a candidate either way
            return job.status == JobStatus.COMPLETED
    if remembered.get("pre") is None:
        return True
    return path_fingerprint(remembered["path"]) != remembered["pre"]


async def _persist_known_outcomes(
    library: Library, platform_id: str, converted: dict,
) -> dict:
    """Freeze into the record every outcome the queue can still answer for.

    The queue's memory is the short-lived half of this: it is capped, and a
    restart empties it. Asking it during the sweep and storing what it says
    turns an answer that expires into one that does not, so a later sweep is
    not left guessing from the filesystem.

    Returns the (possibly updated) map so the caller keeps using one copy.
    Always called from inside ``_sweep_lock``.
    """
    updates = {}
    for rom_id, record in converted.items():
        if not isinstance(record, dict) or "done" in record:
            continue
        job_id = record.get("job_id")
        if not job_id:
            continue
        job = job_manager.get_job(job_id)
        if job is None or job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
            continue
        updates[rom_id] = job.status == JobStatus.COMPLETED
    if not updates:
        return converted
    state = await get_state(library)
    entry = dict(state.get(str(platform_id)) or {})
    stored = dict(entry.get("converted") or {})
    for rom_id, done in updates.items():
        record = dict(stored.get(rom_id) or converted[rom_id])
        record["done"] = done
        stored[rom_id] = record
        converted[rom_id] = record
    entry["converted"] = stored
    state[str(platform_id)] = entry
    await store.write(store.STATE_KEY, library, state)
    return converted


async def note_job_finished(job) -> None:
    """Write down how *job* ended, for the rule that queued it.

    Registered with ``job_manager.add_terminal_listener`` at startup, and the
    reason the history above is trustworthy at all. Job history is capped and
    lives in memory: ask ``get_job()`` after a prune or a restart and the
    answer is "unknown", at which point the only evidence left is the
    destination having changed -- which a *failed* overwrite produces just as
    well as a successful conversion, by unlinking the old artifact or leaving
    a partial one. That ROM would then be skipped by every later sweep until
    the operator pressed **Forget history**.

    So the answer is recorded at the one moment it is certain. Idempotent: the
    same outcome may be announced twice (a queued job cancelled before it
    starts hears it from both `cancel_job` and `_process_job`), and writing
    the same verdict again is a no-op.
    """
    status = getattr(job, "status", None)
    if status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return
    job_id = getattr(job, "id", None)
    if not job_id:
        return
    # Whose record is this? Answered from the index this process filled when it
    # queued the job, not by walking the history: the listener fires for every
    # job in the app, most of them manual, and scanning every remembered ROM of
    # every platform per completion is O(history) each time -- on the event
    # loop, holding `_sweep_lock`, so a batch of completions against a large
    # history delays every unrelated request behind it.
    #
    # A miss is not a fallback case: a record can only name a job this process
    # queued (the queue is in memory and does not survive a restart), and
    # `_mark_converted` indexes every one of those. So a miss means the job is
    # not ours, and there is nothing to write.
    owner = _job_owners.pop(job_id, None)
    decision = _job_decision.pop(job_id, None)
    if owner is None:
        return
    library, platform_id, rom_id, identity, queued_fingerprint = owner
    done = status == JobStatus.COMPLETED
    # Captured before any await; see `_generations`.
    generation = _generations.get(library.id, 0)
    unfinished = getattr(job, "_unfinished_delete", None)
    if unfinished:
        # Its delete stopped part way: finish it like a kept Source, from the
        # files still to remove (codex 4580227623).
        await _remember_kept_source(
            library, platform_id, rom_id, job, generation, decision,
            unfinished=list(unfinished),
        )
    elif (
        done and getattr(job, "delete_on_verify", False)
        and getattr(job, "warning", None) == SOURCE_KEPT_FOR_READERS
    ):
        await _remember_kept_source(library, platform_id, rom_id, job, generation, decision)
    if (
        FAILED_HOLD in library.capabilities and status != JobStatus.CANCELLED
        and await _same_rule(library, platform_id, identity)
    ):
        await _note_failure(
            library, platform_id, rom_id, job, failed=not done, generation=generation,
            identity=identity, queued_fingerprint=queued_fingerprint,
        )
    if CONVERTED_HISTORY not in library.capabilities:
        # No converted history to write the verdict into (skip-only Rules).
        return
    async with _sweep_lock:
        if _generations.get(library.id, 0) != generation:
            return  # removed meanwhile
        # Under the same lock as every other write to this blob: a sweep
        # rewrites the whole thing, so an unsynchronised update here would be
        # lost, or would lose the sweep's.
        state = await get_state(library)
        converted = ((state.get(platform_id) or {}).get("converted") or {})
        record = converted.get(rom_id)
        if record is None and rom_id not in converted:
            # Queued, but the record has not been persisted yet -- the owner is
            # registered at acceptance and the record lands a few awaits later.
            # Leave the answer for `_mark_converted` to pick up rather than
            # dropping it: without it the row keeps no verdict, and after a
            # restart the only evidence left is the destination having changed,
            # which a *failed* overwrite produces just as convincingly.
            _late_verdicts[job_id] = done
            return
        # Re-checked rather than trusted: `forget_converted` may have cleared
        # the history since, and a re-planned conversion may have replaced the
        # record with one naming a different job.
        if not isinstance(record, dict) or record.get("job_id") != job_id:
            return
        if record.get("done") == done:
            return
        record = dict(record)
        record["done"] = done
        state[platform_id]["converted"][rom_id] = record
        await store.write(store.STATE_KEY, library, state)


async def _remember_kept_source(
    library: Library, platform_id: str, image_key: str, job, generation: int,
    decision: tuple[dict, int] | None, *, unfinished: list[str] | None = None,
) -> None:
    """Note a Source the delete step kept for a reader, for a later sweep to
    retry (#329). Any Library: it needs no converted history.

    *decision* is what the job's delete was decided under, stamped when it was
    queued: the delete decision identity and the decision generation. Recorded
    only while both still hold, checked under ``_sweep_lock``: a job that ends
    after its Rule, its Library or its history changed (a retarget, a new
    check level, Forget history, RomM's identity cleanup) records nothing
    (codex 4579636593, 4579636594).

    *unfinished* is a delete that stopped part way: the files still to
    remove. Recorded as ``state: deleting`` to be finished; when it cannot be
    recorded it is reported (a ``source_kept`` Finding), never dropped.
    """
    if decision is None:
        if unfinished:
            await _report_unfinished(library, image_key, job, unfinished)
        return  # not decided by a Rule this process queued: never retried
    identity, queued_generation = decision
    pending = {
        "source": getattr(job, "file_path", None),
        "output": getattr(job, "output_path", None),
        "job_id": getattr(job, "id", None),
        "kept_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempts": 0,
        "decision": identity,
        "decision_generation": queued_generation,
    }
    if unfinished:
        pending.update({"state": KEPT_DELETING, "planned": list(unfinished)})
    if not pending["source"] or not pending["output"]:
        return
    recorded = False
    async with _sweep_lock:
        if _generations.get(library.id, 0) != generation:
            # Removed meanwhile: nothing goes back into its state, and a
            # started delete is reported under no Library (codex 4580441301).
            if unfinished:
                await _report_unfinished(
                    library, image_key, job, unfinished, owner=findings.NO_LIBRARY,
                )
            return
        state = await get_state(library)
        entry = dict(state.get(platform_id) or {})
        rule = (await get_rules(library)).get(platform_id)
        if (
            # Invalidated, or the Rule or the Library changed meanwhile.
            _decision_generation(entry) == queued_generation
            and _delete_decision_identity(rule, library) == identity
        ):
            kept = dict(entry.get(KEPT_SOURCES) or {})
            kept[str(image_key)] = pending
            entry[KEPT_SOURCES] = kept
            state[platform_id] = entry
            await store.write(store.STATE_KEY, library, state)
            recorded = True
    if unfinished and not recorded:
        await _report_unfinished(library, image_key, job, unfinished)


async def _report_unfinished(
    library: Library, image_key: str, job, unfinished: list[str],
    *, owner: str | None = None,
) -> None:
    """A part-deleted Source no pending delete can finish: say so (under
    *owner* ``findings.NO_LIBRARY`` once its Library is gone)."""
    await _report_started_delete(
        library, image_key,
        {"source": getattr(job, "file_path", None), "output": getattr(job, "output_path", None)},
        "its delete can no longer be finished", unfinished, owner=owner,
    )


# Every Rule field that decides whether a delete is allowed (#329): what the
# job produces (``OUTPUT_IDENTITY_FIELDS``), whether it deletes at all, and
# the check it must pass first. ``_delete_decision_identity`` adds the
# effective check level and the Library.
DELETE_DECISION_FIELDS = (*OUTPUT_IDENTITY_FIELDS, "delete_on_verify", "check_level")
DECISION_GENERATION = "decision_generation"
# A Platform's Rule revision, in its Rule state entry: moved on by every
# `set_rules` change to that Platform's Rule, whatever the field (codex
# 4580441297). A pending delete tried off `_sweep_lock` deletes only while it
# is the one the try was planned under, so no field (a selection filter
# included) can change under a delete unnoticed.
RULE_REVISION = "rule_revision"


def _rule_revision(entry: dict | None) -> int:
    """A Platform's Rule revision, from its Rule state entry."""
    try:
        return int((entry or {}).get(RULE_REVISION) or 0)
    except (TypeError, ValueError):
        return 0


def _delete_decision_identity(rule: dict | None, library: Library) -> dict:
    """Every input to a Rule's delete decision, for a pending delete (#329).

    The Rule's ``DELETE_DECISION_FIELDS``, the check its job passes before
    deleting as the registry answers it now (``effective_check_level``), and
    the Library (``_library_identity``). A kept Source is deleted only while
    this is unchanged.
    """
    rule = rule or {}
    return {
        "rule": {field: rule.get(field) for field in DELETE_DECISION_FIELDS},
        "check": effective_check_level(rule) if rule else None,
        "library": _library_identity(library),
    }


def _decision_generation(entry: dict | None) -> int:
    """A Platform's decision generation, from its Rule state entry."""
    try:
        return int((entry or {}).get(DECISION_GENERATION) or 0)
    except (TypeError, ValueError):
        return 0


async def _invalidate_decisions(
    library: Library, state: dict, platform_ids, *, owner: str | None = None,
) -> bool:
    """Move each Platform's decision generation on and drop its pending
    deletes, in *state* (written by the caller). True when anything changed.

    A pending delete that has not started is dropped as it is: the Source is
    whole. One whose unlinks began (``deleting``) is reported first, with the
    files it left (codex 4580328581), under *owner* when the Library itself
    will not hold the Finding (its removal, RomM's identity cleanup).
    """
    for pid in platform_ids:
        entry = dict(state.get(str(pid)) or {})
        for image_key, pending in dict(entry.get(KEPT_SOURCES) or {}).items():
            if isinstance(pending, dict) and pending.get("state") == KEPT_DELETING:
                await _report_started_delete(
                    library, image_key, pending,
                    "the Rule or Library its delete was decided under changed",
                    [str(path) for path in pending.get("planned") or []],
                    owner=owner,
                )
        entry[DECISION_GENERATION] = _decision_generation(entry) + 1
        entry.pop(KEPT_SOURCES, None)
        state[str(pid)] = entry
    return bool(platform_ids)


# What the sweep's own plan says of a pending delete's Source (`_current_plan`).
PLAN_SAME = "same"  # selected, and converted by this Rule to the same output
PLAN_OTHER = "other"  # not selected, or planned differently: never delete it
PLAN_UNANSWERED = "unanswered"  # could not be read now: an attempt


def _planned_delete(image: Image, rule: dict, tool, spec, source: str, output: str) -> str:
    """The sweep's own plan for *image* under *rule*, set against a pending
    delete of *source* to *output*. Blocking.

    ``_inspect_candidate`` is the function the sweep decides with: name, size
    and match filters, containment, the mode's input check, the Source's
    companions, and the destination the Rule maps it to. The output exists by
    now, so the destination is asked as ``overwrite`` would (where this Rule
    writes it, not whether to write it again); the duplicate policy itself is
    a decision-identity input. Under the ``rename`` policy the output may be
    the numbered sibling the rename gave that destination (``Game_1.rvz``,
    ``is_renamed_from``, codex 4580078019); ``overwrite`` and ``skip`` take
    the destination exactly. Either way the retry then requires the check
    record of that very output to name this Source.
    """
    plan = _inspect_candidate(
        image, {**rule, "duplicate_action": "overwrite"}, tool, spec, None,
    )
    if plan["skip"] == SKIP_LOCKED:
        return PLAN_UNANSWERED  # the output is busy right now: ask again later
    if plan["skip"] is not None or not plan["destination"] or not plan["path"]:
        return PLAN_OTHER
    planned = os.path.realpath(plan["destination"])
    recorded = os.path.realpath(output)
    same_output = recorded == planned or (
        rule["duplicate_action"] == "rename"
        and is_renamed_from(recorded, planned, rule["mode"])
    )
    same = same_output and os.path.realpath(plan["path"]) == os.path.realpath(source)
    return PLAN_SAME if same else PLAN_OTHER


async def _current_plan(
    library: Library, platform_id: str, rule: dict, image_key: str, pending: dict,
    listings: dict, stopping: Callable[[], bool],
) -> str:
    """Whether the sweep, planning now, would still convert this kept Source
    to the same output with its delete on: ``PLAN_SAME``, ``PLAN_OTHER`` or
    ``PLAN_UNANSWERED``. Lists the Platform's Images once (*listings*) and asks
    the sweep's own ``_inspect_candidate``, both bounded and off the loop."""
    try:
        spec = registry.spec(rule["mode"])
        tool = registry.for_mode(rule["mode"])
    except KeyError:
        return PLAN_OTHER
    if platform_id not in listings:
        listings[platform_id] = await _list_images(
            library, platform_id, {"errors": [], "stopped_reason": None}, stopping,
        )
    images = listings[platform_id]
    if images is None:
        return PLAN_UNANSWERED
    image = next((i for i in images if str(i.key) == image_key), None)
    if image is None or not registry.mode_allows_platform(rule["mode"], image.platform):
        return PLAN_OTHER
    return await _probe(
        _planned_delete, image, rule, tool, spec,
        str(pending.get("source") or ""), str(pending.get("output") or ""),
        default=PLAN_UNANSWERED,
    )


KEPT_DELETING = "deleting"  # a pending delete's state once its unlinks began


async def _report_kept_source(
    library: Library, image_key: str, pending: dict, reason: str, path: str,
) -> None:
    """File the ``source_kept`` Finding for a pending delete that never
    started, given up on (its Source is whole)."""
    await _file_kept_finding(library, findings.Observed(
        kind=findings.SOURCE_KEPT, image_key=str(image_key), path=path, reason=reason,
        cause={"source": pending.get("source"), "output": pending.get("output")},
    ))


async def _report_started_delete(
    library: Library, image_key: str, pending: dict, why: str, left: list[str],
    *, owner: str | None = None,
) -> None:
    """The one Finding for a started delete that will not be finished: the
    output no longer stands, a Rule or Library change invalidated it, or it
    ran out of tries (``findings.started_delete``, which names the files
    left). *owner* ``findings.NO_LIBRARY`` files it under no Library, keyed by
    the Source's path: a Library's Image key means nothing without it."""
    source = pending.get("source")
    if owner == findings.NO_LIBRARY:
        image_key = str(source or image_key)
    await _file_kept_finding(library, findings.started_delete(
        image_key, source=source, output=pending.get("output"), why=why, left=left,
    ), owner=owner)


async def _file_kept_finding(
    library: Library, item: findings.Observed, *, owner: str | None = None,
) -> None:
    """Write *item* under *library*, bound to its generation (codex
    4580078026), or under *owner* ``findings.NO_LIBRARY``, bound to none."""
    if owner == findings.NO_LIBRARY:
        await _probe(findings.observe, owner, item, default=None)
        return
    generation = _generations.get(library.id, 0)
    await _probe(
        functools.partial(
            findings.observe,
            current=lambda: _generations.get(library.id, 0) == generation,
        ),
        library.id, item, default=None,
    )


async def _kept_outcome(
    library: Library, image_key: str, pending: dict, outcome: KeptDeleteRetry,
    result: dict,
) -> dict | None:
    """What one pending delete becomes after *outcome*: the entry to keep, or
    None when it goes (deleted, given up on, or no longer standing).

    A started delete (``deleting``, or an outcome saying it started) that
    will not be finished is reported with the files it left
    (``_report_started_delete``); a whole Source given up on at the attempt
    cap gets the ``source_kept`` Finding as well.
    """
    deleting = pending.get("state") == KEPT_DELETING
    planned = [str(path) for path in pending.get("planned") or []]
    if outcome.status == KEPT_DELETED:
        result["kept_sources_deleted"] = result.get("kept_sources_deleted", 0) + 1
        return None
    if outcome.status in (KEPT_UNKNOWN, KEPT_STARTED):
        # The delete started: the unlinks outlived the wait (the worker
        # removes the rest of the plan), one failed after another succeeded,
        # or a cancel stopped it. Not an attempt; the next sweep finishes it
        # from the recorded plan, not a new one (codex 4580176201, 4580227623).
        return {
            **pending, "state": KEPT_DELETING,
            "planned": list(outcome.planned) or planned, "last_reason": outcome.reason,
        }
    if outcome.status == KEPT_STALE:
        if deleting:
            # The output no longer stands: the files left stay, and say so.
            await _report_started_delete(
                library, image_key, pending, str(outcome.reason),
                list(outcome.planned) or planned,
            )
        else:
            logger.info(
                "rules: not retrying the delete of %s: %s",
                pending.get("source"), outcome.reason,
            )
        return None
    attempts = int(pending.get("attempts") or 0) + 1
    if attempts < KEPT_DELETE_MAX_ATTEMPTS:
        return {**pending, "attempts": attempts, "last_reason": outcome.reason}
    if deleting:
        await _report_started_delete(
            library, image_key, pending,
            f"still in use after {attempts} tries to finish deleting it ({outcome.reason})",
            list(outcome.planned) or planned,
        )
        return None
    result["kept_sources_given_up"] = result.get("kept_sources_given_up", 0) + 1
    await _report_kept_source(
        library, image_key, pending,
        f"Source kept: still in use after {attempts} tries to delete it "
        f"({outcome.reason})",
        str(pending.get("source") or ""),
    )
    return None


def _rule_paused(rule: dict, wanted: set[str] | None) -> bool:
    """The `enabled` gate (see `_sweep_locked`): an operator's pause holds
    unless the Platform is named, and a validation pause holds always."""
    return not rule["enabled"] and (wanted is None or _validation_paused(rule))


def _library_identity(library: Library) -> list:
    """What a Library is, for a pending delete (#329): its id and root, and
    whatever else names it (``decision_identity``: RomM's instance URL). A
    Source kept under one of these is deleted only while it is the same."""
    extra = getattr(library, "decision_identity", None)
    root = getattr(library, "root", None)
    return [library.id, str(root) if root else None, *(extra() if callable(extra) else ())]


def _inside_library(source: str, root: str | None) -> bool:
    """Whether *source* is inside *root* as both resolve now. Blocking."""
    if not root or not source:
        return False
    real_root = os.path.realpath(root)
    real_source = os.path.realpath(source.split("::", 1)[0])
    try:
        return os.path.commonpath([real_root, real_source]) == real_root
    except ValueError:
        return False


@dataclass(frozen=True)
class _KeptAttempt:
    """One pending delete a sweep tries, planned under ``_sweep_lock`` and run
    after it (:func:`_run_kept_deletes`)."""

    platform_id: str
    image_key: str
    pending: dict
    required_check: str | None
    generation: int
    revision: int

    def key(self, library: Library) -> tuple[str, str, str]:
        return (library.id, self.platform_id, self.image_key)


# The pending deletes a sweep is running now, off `_sweep_lock` (its check of
# the output is long): another sweep leaves each alone until it is done.
_kept_running: set[tuple[str, str, str]] = set()


async def _plan_kept_deletes(
    library: Library, rules: dict, wanted: set[str] | None,
    stopping: Callable[[], bool], result: dict,
) -> list[_KeptAttempt]:
    """Decide which Sources a reader kept are tried this sweep (#329). Called
    under ``_sweep_lock``; :func:`_run_kept_deletes` tries them after it.

    Only for a Platform whose conversion Rule still deletes its Sources: a
    Rule turned off or changed to keep them drops the entry. What no longer
    stands (the decision, the Library, the sweep's own plan) is dropped here,
    with the Source kept; an unanswered volume is an attempt. A delete that
    already started (``deleting``) is always tried, to finish it.
    """
    state = await get_state(library)
    budget = KEPT_DELETES_PER_SWEEP
    changed = False
    attempts: list[_KeptAttempt] = []
    generation = _generations.get(library.id, 0)
    for platform_id in sorted(state):
        entry = state[platform_id]
        kept = entry.get(KEPT_SOURCES) if isinstance(entry, dict) else None
        if not kept or (wanted is not None and platform_id not in wanted):
            continue
        rule = rules.get(platform_id)
        if rule and _rule_paused(rule, wanted):
            # The same pause the Platform pass honours: its entries wait,
            # neither retried nor dropped (codex 4579529040).
            continue
        remaining = dict(kept)
        listings: dict = {}  # this Platform's Images, listed once if needed
        for image_key in sorted(kept):
            if stopping() or budget <= 0:
                break
            if (library.id, platform_id, image_key) in _kept_running:
                continue  # another sweep is trying it right now
            pending = kept[image_key]
            required = effective_check_level(rule) if rule else None
            if isinstance(pending, dict) and pending.get("state") == KEPT_DELETING:
                # A delete already under way: finish it, never plan it again.
                budget -= 1
                attempts.append(_KeptAttempt(
                    platform_id, image_key, pending, required, generation,
                    _rule_revision(entry),
                ))
                continue
            if (
                not rule or not rule.get("delete_on_verify")
                or not isinstance(pending, dict)
                or pending.get("decision") != _delete_decision_identity(rule, library)
                or pending.get(DECISION_GENERATION) != _decision_generation(entry)
            ):
                # The Rule is gone or keeps its Sources now, or anything the
                # delete was decided on changed (the Rule, the check, the
                # Library), or it was invalidated: that decision no longer
                # stands.
                remaining.pop(image_key)
                continue
            # And the Source is still inside the Library as it is now, checked
            # fresh (bounded; an unanswered volume is an attempt, not a verdict).
            inside = await _probe(
                _inside_library, str(pending.get("source") or ""),
                getattr(library, "root", None), default=None,
            )
            if inside is False:
                remaining.pop(image_key)
                continue
            # The authority: the sweep's own selection and plan for this one
            # Source under the current Rule and Library. Deleted only while
            # that plan is still this conversion to this output (codex
            # 4580012814); the identity above is the fast pre-check.
            plan = await _current_plan(
                library, platform_id, rule, str(image_key), pending, listings, stopping,
            )
            if plan == PLAN_OTHER:
                remaining.pop(image_key)
                continue
            budget -= 1
            if inside is None or plan == PLAN_UNANSWERED:
                reason = (
                    "the Library's volume did not answer" if inside is None
                    else "the current plan for this Source could not be read"
                )
                kept_entry = await _kept_outcome(
                    library, image_key, pending, KeptDeleteRetry(KEPT_KEPT, reason), result,
                )
                if kept_entry is None:
                    remaining.pop(image_key)
                else:
                    remaining[image_key] = kept_entry
                continue
            attempts.append(_KeptAttempt(
                platform_id, image_key, pending, required, generation,
                _rule_revision(entry),
            ))
        if remaining != kept:
            entry = dict(entry)
            if remaining:
                entry[KEPT_SOURCES] = remaining
            else:
                entry.pop(KEPT_SOURCES, None)
            state[platform_id] = entry
            changed = True
    if changed:
        await store.write(store.STATE_KEY, library, state)
    # A Source deleted by hand after the sweep gave up: its Finding goes.
    await _probe(
        functools.partial(
            findings.clear_gone,
            current=lambda: _generations.get(library.id, 0) == generation,
        ),
        library.id, findings.SOURCE_KEPT, default=0,
    )
    for attempt in attempts:
        _kept_running.add(attempt.key(library))
    return attempts


async def _still_pending(library: Library, attempt: _KeptAttempt) -> bool:
    """Whether *attempt*'s entry is still the one the sweep planned: the same
    Library generation, the same Rule revision, and the very same entry. A
    Rule edit of any field, Forget history, RomM's identity cleanup or the
    Library's removal while it was checked changed it (an invalidation also
    reported a started delete); the next sweep plans it again. Under
    ``_sweep_lock``: the authority right before the unlinks."""
    if _generations.get(library.id, 0) != attempt.generation:
        return False
    entry = (await get_state(library)).get(attempt.platform_id)
    if _rule_revision(entry) != attempt.revision:
        return False
    kept = entry.get(KEPT_SOURCES) if isinstance(entry, dict) else None
    return isinstance(kept, dict) and kept.get(attempt.image_key) == attempt.pending


async def _settle_kept(
    library: Library, attempt: _KeptAttempt, outcome: KeptDeleteRetry, result: dict,
) -> None:
    """Write what *attempt*'s entry becomes after *outcome*. Under ``_sweep_lock``."""
    kept_entry = await _kept_outcome(
        library, attempt.image_key, attempt.pending, outcome, result,
    )
    state = await get_state(library)
    entry = dict(state.get(attempt.platform_id) or {})
    kept = dict(entry.get(KEPT_SOURCES) or {})
    if kept_entry is None:
        kept.pop(attempt.image_key, None)
    else:
        kept[attempt.image_key] = kept_entry
    if kept:
        entry[KEPT_SOURCES] = kept
    else:
        entry.pop(KEPT_SOURCES, None)
    state[attempt.platform_id] = entry
    await store.write(store.STATE_KEY, library, state)


async def _run_kept_deletes(
    library: Library, attempts: list[_KeptAttempt], result: dict,
    stopping: Callable[[], bool] = lambda: False,
) -> None:
    """Try each planned pending delete, after the sweep let go of
    ``_sweep_lock`` (codex 4580346573).

    Each is prepared under its own file locks, held throughout
    (``job_manager.kept_delete``): the record, the Source, and the output's
    check run again, which can take as long as a verify. Only then is
    ``_sweep_lock`` taken, to confirm the entry still stands, delete, and
    record the outcome, so a long check never holds up other sweeps, Rule
    edits or job completions.

    A stop (*stopping*: ``run-rules`` on SIGTERM, codex 4580441303) is asked
    before each try, before its output check, and before its unlinks: once it
    is true nothing new starts and no unlink begins, and nothing is recorded
    for what was not tried. Unlinks already handed to their worker carry on,
    recorded as in flight.
    """
    for attempt in attempts:
        try:
            if not stopping():
                await _run_kept_delete(library, attempt, result, stopping)
        finally:
            _kept_running.discard(attempt.key(library))
    if attempts and stopping():
        result["stopped_reason"] = result.get("stopped_reason") or "stopped"


async def _run_kept_delete(
    library: Library, attempt: _KeptAttempt, result: dict,
    stopping: Callable[[], bool] = lambda: False,
) -> None:
    """One :func:`_run_kept_deletes` attempt."""
    pending = attempt.pending
    deleting = pending.get("state") == KEPT_DELETING
    try:
        async with job_manager.kept_delete(
            source_path=str(pending.get("source") or ""),
            output_path=str(pending.get("output") or ""),
            required_check=attempt.required_check,
            planned=(
                [str(path) for path in pending.get("planned") or []] if deleting else None
            ),
            stopping=stopping,
        ) as run:
            if run.refused is None:
                async with _sweep_lock:
                    if stopping() or not await _still_pending(library, attempt):
                        return
                    try:
                        outcome = await run.delete()
                    except asyncio.CancelledError:
                        # Stopped once the unlinks began (CodeRabbit
                        # 4580360089): what started is recorded, bounded,
                        # then the cancel goes on.
                        if run.started is not None:
                            await _record_cancelled_delete(library, attempt, run.started, result)
                        raise
                    if outcome.status != KEPT_STOPPED:
                        await _settle_kept(library, attempt, outcome, result)
                return
            outcome = run.refused
            if outcome.status == KEPT_STOPPED:
                return  # not tried: nothing to record
    except Exception as exc:
        # One entry's failure (an unlink refused, an I/O error) is that
        # entry's attempt, never the sweep's (codex 4579529048).
        logger.warning(
            "rules: retrying the delete of %s failed", pending.get("source"), exc_info=True,
        )
        outcome = KeptDeleteRetry(KEPT_KEPT, str(exc) or type(exc).__name__)
    async with _sweep_lock:
        if await _still_pending(library, attempt):
            await _settle_kept(library, attempt, outcome, result)


async def _record_cancelled_delete(
    library: Library, attempt: _KeptAttempt, started: KeptDeleteRetry, result: dict,
) -> None:
    """Record a delete a cancel stopped after it began, as ``deleting``,
    bounded: the cancel is already on its way, and never waits on the
    unlink worker."""
    try:
        await asyncio.wait_for(
            _settle_kept(library, attempt, started, result), _CANCEL_RECORD_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "rules: recording the cancelled delete of %s did not finish",
            attempt.pending.get("source"), exc_info=True,
        )


async def _prune_holds(
    library: Library, platform_id: str, held: dict, images: list[Image],
) -> dict:
    """Drop holds for Images *library* no longer lists. Returns the holds kept."""
    listed = {str(i.key) for i in images}
    kept = {key: value for key, value in held.items() if key in listed}
    if len(kept) == len(held):
        return held
    state = await get_state(library)
    entry = dict(state.get(platform_id) or {})
    entry["failed"] = kept
    state[platform_id] = entry
    await store.write(store.STATE_KEY, library, state)
    return kept


async def _note_failure(
    library: Library, platform_id: str, image_key: str, job, *, failed: bool,
    generation: int, identity: tuple | None = None,
    queued_fingerprint: str | None = None,
) -> None:
    """Hold a failed Image until its file changes, or release a converted one.

    Only for a Library with :data:`FAILED_HOLD`. The hold is the Source's
    fingerprint (size and mtime of its files); the sweep compares it with the
    files' now. A cancelled job is neither: it says nothing about the Image.

    The hold is bound to the Source the job converted: *queued_fingerprint*,
    read when the job was queued. A Source that no longer matches it (repaired
    after the failure, before this read) never failed, so nothing is held.
    And it is bound to the Rule that queued the job: *identity* is checked
    again under the write lock, since the Rule can be retargeted while the
    fingerprint is being read.
    """
    fingerprint = None
    if failed:
        source = getattr(job, "file_path", None)
        if not source:
            return
        fingerprint = await _probe(image_fingerprint, source, default="")
        if not fingerprint:
            return
        if queued_fingerprint is not None and fingerprint != queued_fingerprint:
            return
    async with _sweep_lock:
        if _generations.get(library.id, 0) != generation:
            # The Library was removed while the fingerprint was being read.
            return
        if not await _same_rule(library, platform_id, identity):
            # Retargeted while the fingerprint was being read.
            return
        state = await get_state(library)
        entry = dict(state.get(platform_id) or {})
        held = dict(entry.get("failed") or {})
        if failed:
            held[image_key] = {"fingerprint": fingerprint, "job_id": getattr(job, "id", None)}
        elif image_key in held:
            held.pop(image_key)
        else:
            return
        entry["failed"] = held
        state[platform_id] = entry
        await store.write(store.STATE_KEY, library, state)


async def _same_rule(library: Library, platform_id: str, identity: tuple | None) -> bool:
    """Whether *platform_id*'s Rule still produces what the job was queued for.

    A job queued under one Rule can end after the Rule is retargeted (RVZ to
    WIA). Its outcome says nothing about the new target, so it must not hold
    or release an Image under it. None (an owner registered without one) is
    taken as the same Rule.
    """
    if identity is None:
        return True
    rule = (await get_rules(library)).get(platform_id)
    return _output_identity(rule) == identity


def _own_jobs(
    library: Library, platform_id: str, entries: dict, identity: tuple | None = None,
    *, fingerprints: dict[str, str | None] | None = None,
    decision: tuple[dict, int] | None = None,
) -> None:
    """Register `note_job_finished`'s owner map for these queued conversions.

    Called the instant `create_batch_jobs` returns, with no await in between,
    because the listener fires from the queue worker: a fast conversion on an
    idle queue can finish while the post-queue bookkeeping is still running,
    and an outcome that arrives before its owner is registered is discarded.
    The record then carries no verdict, and after a restart the only evidence
    left is the destination having changed -- true of a *failed* overwrite too,
    so the ROM is skipped by every later sweep until **Forget history**.

    Entries are removed as their jobs finish; one that never reaches a terminal
    status keeps a single small tuple, the same exposure the job itself already
    has in the queue.
    """
    for rom_id, record in entries.items():
        if record.get("job_id"):
            job_id = str(record["job_id"])
            # A re-registration without an identity or fingerprint
            # (`_mark_converted`) keeps the ones recorded when the job was queued.
            previous = _job_owners.get(job_id)
            own_identity = identity
            if own_identity is None and previous is not None:
                own_identity = previous[3]
            fingerprint = (fingerprints or {}).get(str(rom_id))
            if fingerprint is None and previous is not None:
                fingerprint = previous[4]
            _job_owners[job_id] = (
                library, str(platform_id), str(rom_id), own_identity, fingerprint,
            )
            if decision is not None:
                _job_decision.setdefault(job_id, decision)


def _converted_entries(produced: list[tuple]) -> dict:
    """``{rom_id: record}`` for the conversions a sweep just queued."""
    return {
        str(rid): {"path": dest, "pre": pre, "job_id": job_id}
        for rid, dest, pre, job_id in produced if rid is not None
    }


async def _mark_converted(library: Library, platform_id: str, entries: dict) -> None:
    """Persist what this rule just produced, one record per queued ROM."""
    if not entries:
        return
    # Idempotent: ownership is registered at acceptance, and re-registering the
    # same pairing costs a dict write.
    _own_jobs(library, platform_id, entries)
    # Verdicts that arrived before this record existed. Ownership is registered
    # the moment the queue accepts the batch, but the listener it feeds writes
    # into a *record* that is not persisted until here -- so a fast conversion
    # on an idle queue can end in between, find its owner, find no record, and
    # have nothing to write to. It leaves the answer here instead.
    for record in entries.values():
        verdict = _late_verdicts.pop(str(record.get("job_id") or ""), None)
        if verdict is not None:
            record["done"] = verdict
    state = await get_state(library)
    entry = dict(state.get(str(platform_id)) or {})
    merged = dict(entry.get("converted") or {})
    merged.update(entries)
    entry["converted"] = merged
    state[str(platform_id)] = entry
    await store.write(store.STATE_KEY, library, state)


def _inspect_candidate(
    image: Image, rule: dict, tool, spec, remembered: dict | None,
    failed: dict | None = None, *, fingerprint_source: bool = False,
) -> dict:
    """Every disk-touching question about one candidate, answered in one hop.

    Mapping the Image onto a local path (for RomM, the catalog record onto the
    mount), the volume check, the input
    predicate, path resolution and the duplicate-policy destination all stat
    the filesystem — and on the NFS/SMB/rclone deployments this integration
    explicitly supports, a mount that stops answering would otherwise block the
    event loop for the length of the catalog.

    The name filters run here too, and first. They touch no disk, but they
    evaluate an operator-supplied regex against operator-supplied names, and
    a pattern like ``(a+)+$`` backtracks for minutes on one long name -- on
    the event loop that is the whole app, not one sweep. Answering ahead of
    the path mapping also means a filtered-out ROM still costs no disk work.

    ``skip`` is None when the ROM should be queued, otherwise the reason.
    ``resolved`` is still filled in whenever it could be, because the caller
    checks it against the in-flight set before acting on ``skip``.
    ``fingerprint`` is the Source's ``image_fingerprint`` when it was read (a
    held Image, or *fingerprint_source* for a Library that holds failures):
    the version a queued job converts, which a failure hold is bound to.
    """
    miss = {
        "skip": "unresolvable", "resolved": None, "destination": None,
        "path": None, "pre": None, "fingerprint": None,
    }
    if not _passes_filters(image, rule):
        return {**miss, "skip": "filtered"}
    # Already produced by this rule. Asked here rather than on the event loop
    # because the answer is a stat of the destination this rule last chose --
    # queueing is not producing, so the record alone does not settle it.
    if _was_produced(remembered):
        return {**miss, "skip": SKIP_EXISTING}
    path = image.path
    if not path or not is_within_configured_volumes(path):
        return miss
    if not _accepts_source(tool, spec, path):
        return {**miss, "skip": "unconvertible", "path": path}
    # The job manager refuses these too; asked here so the sweep reports the
    # Image as unsafe instead of queueing a job that can only be refused. A
    # track sheet naming a file outside the volumes would otherwise have the
    # converter pack that file into its output.
    if not source_companions_are_safe(path, rule["mode"]):
        return {**miss, "skip": "unsafe_source", "path": path}
    resolved = _resolve_source(path)
    if resolved is None:
        return miss
    # RomM's catalog outlives the files it describes: an entry whose ROM was
    # moved or deleted outside RomM still lists a local_path, and queueing it
    # spends a worker slot to fail in the tool. Same stat call either way, so
    # asking here costs nothing the resolve did not already pay for.
    #
    # And what is there has to be the *kind* the mode consumes. The check
    # above is a declaration check -- extensions and the tool's predicate --
    # so a directory whose name carries an accepted extension passes it, and
    # plain existence would then let it through. The manual batch route stats
    # for a regular file; automation must too, because here nothing is
    # watching: an `overwrite` rule authorises the job, `_clear_existing_output`
    # removes the previous artifact, and only then does the converter fail on
    # a directory it cannot open.
    wants_directory = (
        registry.mode_input_kind(rule["mode"]) is InputKind.DIRECTORY
    )
    present = os.path.isdir(resolved) if wants_directory else os.path.isfile(resolved)
    if not present:
        # Nothing there at all is "gone from disk"; something of the wrong kind
        # is the target format refusing it, which is what the operator needs to
        # read to understand why.
        reason = "missing" if not os.path.exists(resolved) else "unconvertible"
        return {**miss, "skip": reason, "path": path}
    # Its last conversion failed, and the Image is the same one: queueing it
    # again would fail the same way. A changed size or mtime of any of its
    # files (a sheet's tracks too) releases it.
    fingerprint = image_fingerprint(path) if failed or fingerprint_source else None
    if failed and failed.get("fingerprint") == fingerprint:
        return {**miss, "skip": "held_failed", "path": path}
    destination, decision = resolve_destination(
        tool, path, rule["mode"], rule["output_dir"], rule["duplicate_action"],
    )
    # The rule's output_dir is validated when the rule is saved, but that is
    # not the only way a destination is chosen: with no output_dir the tool
    # derives one from the source, and a mode can place it beside the source
    # rather than inside it (makeps3iso writes the ISO next to the decrypted
    # folder). Check what was actually resolved -- but only when the candidate
    # is going to be queued, or this would mask the policy's own answer (a
    # skipped destination is reported as None).
    if decision == QUEUE and (
        not destination or not is_within_configured_volumes(destination)
    ):
        return {**miss, "skip": "outside_volumes", "path": path}
    return {
        "path": path,
        "skip": None if decision == QUEUE else decision,
        "resolved": resolved,
        "destination": destination,
        # What occupies the destination right now, so a later sweep can tell
        # "the conversion ran" from "the job never happened".
        "pre": path_fingerprint(destination) if destination else "",
        "fingerprint": fingerprint,
    }


def _active_source_paths() -> set[str]:
    """Sources already queued or converting, or held by a running check (#330),
    resolved once per sweep."""
    out: set[str] = set()
    held = [paths for _job_id, paths in job_manager.get_active_job_candidates()]
    held.append(checks.in_flight_paths())
    for paths in held:
        for candidate in paths:
            try:
                out.add(str(Path(candidate).expanduser().resolve(strict=False)))
            except (OSError, RuntimeError):
                continue
    return out


async def sweep(
    library: Library,
    *,
    platform_ids: list[Any] | None = None,
    ignore_schedule: bool = False,
    dry_run: bool = False,
    overall_limit: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Run one pass over *library*'s ruled Platforms.

    Returns a summary rather than raising on a partial failure: one unreachable
    platform must not abort the sweep for the others.

    *should_stop* is asked before each Platform, before each Image is
    inspected, before each delete snapshot is read, and again just before a
    batch is queued; once it is true the
    sweep queues nothing more and ends with ``stopped_reason == "stopped"``
    (``run-rules`` on SIGTERM, #307).

    Serialised process-wide, so a scheduled tick and a manual run cannot both
    plan against the same "nothing is in flight" snapshot. A preview is a real
    sweep in every respect but the queueing, so it takes the lock too rather
    than reporting candidates a concurrent run is already claiming.
    """
    # The pending deletes this sweep plans under the lock and tries after it
    # (`_run_kept_deletes`): their output checks run without it.
    kept_attempts: list[_KeptAttempt] = []
    # Closed even if the sweep raises after queueing jobs: the default is the
    # failure, replaced by the real result on success. A cancel (shutdown)
    # is not a failure to report.
    notified: dict | None = {"errors": [{"platform_id": None, "error": "sweep_failed"}]}
    result: dict | None = None
    try:
        async with _sweep_lock:
            blocked = library.sweep_blocked()
            if blocked:
                notified = None
                return {
                    "queued": 0, "considered": 0, "platforms": [],
                    "errors": [{"platform_id": None, "error": blocked}],
                    **await library.sweep_extras(),
                }
            result = notified = await _sweep_locked(
                library,
                platform_ids=platform_ids,
                ignore_schedule=ignore_schedule,
                dry_run=dry_run,
                overall_limit=overall_limit,
                should_stop=should_stop,
                kept_attempts=kept_attempts,
            )
        await _run_kept_deletes(
            library, kept_attempts, result,
            lambda: should_stop is not None and should_stop(),
        )
    except asyncio.CancelledError:
        if result is None:
            notified = {"errors": []}
        raise
    finally:
        for attempt in kept_attempts:
            _kept_running.discard(attempt.key(library))
        if notified is not None:
            notifications.sweep_finished(notified, dry_run=dry_run, library_id=library.id)
    # The sweep is finished: every sweep that ran returns through here, after
    # the lock is released. `result["dry_run"]` tells a preview from a run.
    return result


async def _sweep_locked(
    library: Library,
    *,
    platform_ids: list[Any] | None,
    ignore_schedule: bool,
    dry_run: bool,
    overall_limit: int | None,
    should_stop: Callable[[], bool] | None = None,
    kept_attempts: list | None = None,
) -> dict:
    """The sweep body. Always called with ``_sweep_lock`` held. The pending
    deletes it plans go in *kept_attempts*, for the caller to try once the
    lock is released."""
    def stopping() -> bool:
        return should_stop is not None and should_stop()

    # Every Rule of every kind, with its own run state: a Platform's conversion
    # Rule and check Rule run on their own clocks.
    rules = {kind: await get_rules(library, kind) for kind in RULE_KINDS}
    states = {kind: await get_state(library, kind) for kind in RULE_KINDS}
    now = datetime.now(timezone.utc)

    result: dict = {
        "queued": 0,
        "considered": 0,
        **library.summary_fields(),
        "skipped_existing": 0,
        "skipped_active": 0,
        "skipped_filtered": 0,
        "skipped_unconvertible": 0,
        "skipped_missing": 0,
        "platforms": [],
        "errors": [],
        "stopped_reason": None,
        "dry_run": dry_run,
    }
    holds_failures = FAILED_HOLD in library.capabilities
    if holds_failures:
        result["skipped_failed"] = 0
    if not any(rules.values()):
        result["stopped_reason"] = "no_rules"
        return result
    if not library.configured():
        result["stopped_reason"] = "not_configured"
        return result

    configured_cap = int(library.max_per_sweep())
    # A caller-supplied limit narrows the run; it never widens it. The manual
    # endpoints accept one so an operator can try a rule on a handful of ROMs,
    # which is a smaller ask than the configured maximum -- letting it exceed
    # that maximum would make the safety limit advisory, from a button.
    cap = (
        max(1, min(int(overall_limit), configured_cap))
        if overall_limit is not None
        else configured_cap
    )
    # `is not None`, not truthiness. An explicit empty list means "these
    # platforms", and there are none of them -- a multi-select with nothing
    # ticked, forwarded verbatim. Reading it as "no filter" turned a request
    # that selected nothing into a *global* run, queueing every eligible rule
    # including the ones carrying delete-on-verify. Preview widened the same
    # way, so the operator could not even see it coming.
    wanted = {str(p) for p in platform_ids} if platform_ids is not None else None

    # Priority first (lower runs earlier), then id, then kind (conversion
    # first), so the order is total and a capped sweep resumes where the last
    # one stopped.
    ordered = sorted(
        ((kind, pid) for kind in RULE_KINDS for pid in rules[kind]),
        key=lambda item: (
            rules[item[0]][item[1]]["priority"], library.platform_value(item[1]),
            RULE_KINDS.index(item[0]),
        ),
    )
    # Bounded: this resolves every queued job's source, and one of them on a
    # dead mount would hang the sweep with `_sweep_lock` held. An unreadable
    # answer is `None`, which stops the run rather than proceeding blind --
    # without this set the sweep cannot tell a source already converting from
    # one to queue, and under `overwrite` would queue it a second time.
    active_paths = await _probe(_active_source_paths, default=None)
    if active_paths is None:
        result["stopped_reason"] = "library_unresponsive"
        return result
    # Destinations already claimed this sweep. Two sources can resolve to the
    # same output -- a PS3 folder and its sibling ISO, or two entries RomM
    # lists for one file -- and under `overwrite` both resolve as queueable,
    # so without this the second job overwrites the first's output and, with
    # delete_on_verify, both sources are deleted for one surviving file.
    claimed_destinations: set[str] = set()

    # What the check Rules picked, run as one job once the pass is over, and
    # their runs to record then. Both are dropped if a stop comes first.
    picked: list = []
    check_runs: list = []
    if not dry_run:
        await _clear_vanished(library)
        planned_deletes = await _plan_kept_deletes(
            library, rules[CONVERT], wanted, stopping, result,
        )
        if kept_attempts is not None:
            kept_attempts.extend(planned_deletes)
        else:
            for attempt in planned_deletes:
                _kept_running.discard(attempt.key(library))

    for kind, platform_id in ordered:
        if stopping():
            result["stopped_reason"] = "stopped"
            break
        rule = rules[kind][platform_id]
        # The cap counts conversions. A check Rule queues none, so one ordered
        # after a capped conversion Rule still runs.
        if result["queued"] >= cap and kind == CONVERT:
            result["stopped_reason"] = "limit"
            continue
        if wanted is not None and platform_id not in wanted:
            continue
        # How errors and summaries name this Platform (RomM: the integer id).
        platform_ref = library.platform_value(platform_id)
        # `enabled` gates unconditionally: a paused platform must stay paused
        # even when the operator presses Run now, which passes
        # ignore_schedule=True. The one exception is naming platforms
        # explicitly -- that is a deliberate per-platform action, and it keeps
        # the "configure a rule, leave the scheduler off, run it by hand"
        # workflow working.
        #
        # But only for a rule the *operator* paused. A rule paused by
        # validation is a different thing: normalization has already replaced
        # the value it refused with the wider fallback -- an out-of-volume
        # output directory becomes None (write beside every source), a
        # backtracking filter becomes None (match the whole platform), half a
        # window becomes no window -- so running it would do more than the
        # saved rule says, unattended, possibly with delete-on-verify. Those
        # stay paused until the operator fixes what is wrong.
        if _rule_paused(rule, wanted):
            if wanted is not None:
                result["errors"].append(
                    {"platform_id": platform_ref, "error": "rule_invalid"},
                )
            continue
        # ignore_schedule bypasses only the clock (interval, window, weekday).
        if not ignore_schedule and not _is_due(
            rule, states[kind].get(platform_id, {}), now,
        ):
            continue
        if (
            kind == CHECK and not ignore_schedule
            and (library.id, str(platform_id)) in _active_check_runs
        ):
            # Its scheduled run is still checking: one budget per window.
            continue

        if kind == CHECK:
            if await _sweep_check(
                library, platform_id, rule, result, active_paths,
                picked=picked, check_runs=check_runs, dry_run=dry_run,
                manual=ignore_schedule, now=now, stopping=stopping,
            ):
                break
            continue

        try:
            spec = registry.spec(rule["mode"])
            tool = registry.for_mode(rule["mode"])
        except KeyError:
            continue

        # A rule outlives the install it was written against: the editor only
        # offers ready tools, but a saved rule keeps firing after its binary
        # is removed or a container is rebuilt without it, and every job it
        # queues fails at launch. Ask before queueing, and say so.
        if not await _ready_bounded(tool):
            logger.warning(
                "romm_auto: %s is not installed, skipping platform %s",
                spec.tool_id, platform_id,
            )
            result["errors"].append(
                {"platform_id": platform_ref, "error": "tool_not_ready"},
            )
            continue

        # Belt and braces on the destination: `normalize_rule` refuses an
        # out-of-volume output_dir at save time, but a rules blob can also be
        # edited straight in the database.
        if rule["output_dir"] and not await _within_volumes_bounded(
            rule["output_dir"],
        ):
            logger.warning(
                "romm_auto: skipping platform %s, output_dir outside volumes",
                platform_id,
            )
            result["errors"].append(
                {"platform_id": platform_ref, "error": "output_dir_outside_volumes"},
            )
            continue

        listed_at = _walk_generation(library)
        images = await _list_images(library, platform_id, result, stopping)
        if images is None:
            if _sweep_ends(result):
                break
            continue

        # The editor narrows the target list, but a rule can outlive the tool
        # set it was written against (or be hand-edited), and `converts_path` is
        # extension-based -- it cannot tell a GameCube .iso from a PS2 one. Ask
        # the registry, using the Platform slug the Library stamps on each Image.
        slug = next((i.platform for i in images if i.platform), None)
        # Per mode, not per tool. A composite tool is a shell that belongs to
        # no system -- the chain tool has a GameCube mode and a PS2 mode, so it
        # passes a tool-level check on both, and a rule saved against the wrong
        # one would convert a disc to a format for the other console.
        if not registry.mode_allows_platform(rule["mode"], slug):
            logger.info(
                "romm_auto: %s is not a %s mode, skipping platform %s",
                rule["mode"], slug, platform_id,
            )
            result["errors"].append(
                {"platform_id": platform_ref, "error": "tool_wrong_for_platform"},
            )
            continue

        per_platform_cap = min(rule["max_per_run"], cap - result["queued"])
        # Only the destructive policies need provenance: `skip` is already
        # idempotent from the destination alone, and consulting the record
        # there would refuse to reconvert an output the operator deliberately
        # deleted.
        converted = (
            await _converted_map(library, platform_id)
            if rule["duplicate_action"] != "skip"
            else {}
        )
        # Write down any outcome the queue still knows but the record does not.
        # `note_job_finished` is told the moment a job ends, so this normally
        # finds nothing -- it is the backstop for the paths that listener can
        # miss: a job that finished before the sweep wrote its record, or an
        # outcome announced while this process was not the one running.
        converted = await _persist_known_outcomes(library, platform_id, converted)
        held = (
            ((await get_state(library)).get(platform_id) or {}).get("failed") or {}
            if holds_failures else {}
        )
        if held and not dry_run:
            # A hold for an Image the Library no longer lists (deleted, moved)
            # can never be released; drop it. Not on a preview, which writes
            # nothing.
            held = await _prune_holds(library, platform_id, held, images)
        held_count = 0
        unsafe_names: list[str] = []
        batch: list[str] = []
        unresponsive = False
        image_by_path: dict[str, Image] = {}
        destinations: dict[str, str] = {}
        resolved_by_path: dict[str, str] = {}
        pre_by_path: dict[str, str] = {}
        # The Source version each queued job converts (FAILED_HOLD only).
        fingerprint_by_path: dict[str, str | None] = {}
        considered = 0

        for image in sorted(images, key=_image_sort_key(rule)):
            if len(batch) >= per_platform_cap:
                break
            # Per Image too: a Platform whose outputs all exist fills no batch,
            # and every candidate costs its own disk probes.
            if stopping():
                break
            considered += 1
            # One hop to a worker thread for every disk-touching check on this
            # candidate — the local-path mapping and volume check included,
            # since both resolve paths against a mount that may be remote.
            try:
                # Detached and bounded, not a pooled worker: this resolves a
                # path, checks containment, and stats the destination, all on
                # the NFS/SMB/rclone mounts this integration exists for. A
                # mount that stops answering blocks in uninterruptible I/O --
                # which in the shared pool strands a worker per candidate, and
                # holds `_sweep_lock` for the whole wait, so previews, manual
                # runs, and the rule edit that would switch this platform off
                # all queue behind it until a restart.
                decision = await asyncio.wait_for(
                    run_detached(
                        functools.partial(
                            _inspect_candidate, image, rule, tool, spec,
                            converted.get(str(image.key)), held.get(str(image.key)),
                            fingerprint_source=holds_failures,
                        ),
                    ),
                    _CANDIDATE_PROBE_SECONDS,
                )
            except (asyncio.TimeoutError, OSError):
                # One unresponsive candidate means the mount is unresponsive,
                # so the rest of this platform would time out one by one.
                # Stop here and say why; the next sweep starts over.
                logger.warning(
                    "romm_auto: platform %s library stopped responding, "
                    "stopping this platform", platform_id,
                )
                result["errors"].append(
                    {"platform_id": platform_ref, "error": "library_unresponsive"},
                )
                unresponsive = True
                break
            path = decision["path"]
            if decision["skip"] == "filtered":
                result["skipped_filtered"] += 1
                continue
            if decision["skip"] == "unresolvable":
                continue
            if decision["skip"] == "unconvertible":
                result["skipped_unconvertible"] += 1
                continue
            if decision["skip"] == "missing":
                result["skipped_missing"] += 1
                continue
            if decision["skip"] == "outside_volumes":
                result["skipped_unconvertible"] += 1
                continue
            if decision["skip"] == "unsafe_source":
                unsafe_names.append(Path(path).name)
                continue
            if decision["skip"] == "held_failed":
                result["skipped_failed"] += 1
                held_count += 1
                continue
            if decision["resolved"] in active_paths:
                result["skipped_active"] += 1
                continue
            if decision["skip"] == SKIP_EXISTING:
                result["skipped_existing"] += 1
                continue
            if decision["skip"] == SKIP_LOCKED:
                result["skipped_active"] += 1
                continue
            if decision["destination"] in claimed_destinations:
                result["skipped_active"] += 1
                continue
            claimed_destinations.add(decision["destination"])
            batch.append(path)
            image_by_path[path] = image
            destinations[path] = decision["destination"]
            resolved_by_path[path] = decision["resolved"]
            pre_by_path[path] = decision["pre"]
            fingerprint_by_path[path] = decision.get("fingerprint")

        result["considered"] += considered
        summary = {
            "platform_id": platform_ref,
            "mode": rule["mode"],
            "queued": 0,
            # Always present, so a caller reading a preview does not have to
            # tell "nothing recorded" apart from "this key only exists on real
            # runs" (RomM: `repins_recorded`).
            **library.summary_fields(),
            "considered": considered,
        }
        if holds_failures:
            # Shown per Platform, so a held Image does not vanish from the
            # result: it is waiting for its files to change or for Forget.
            summary["skipped_failed"] = held_count
        if unsafe_names:
            # Never queued, and named: a sheet whose tracks leave the volumes.
            # Present only when there is one, so a clean result keeps its shape.
            result["skipped_unsafe"] = result.get("skipped_unsafe", 0) + len(unsafe_names)
            summary["unsafe"] = unsafe_names[:25]
        if rule["delete_on_verify"]:
            # What Preview shows: the check that will actually pass before
            # this batch deletes a Source, not the stored preference (a mode
            # with no digest deletes on Intact). Absent when nothing is deleted.
            summary["check_level"] = effective_check_level(rule)

        if stopping():
            # Asked again here: listing and inspecting a Platform can take
            # minutes, and a stop that arrived meanwhile must queue nothing,
            # nor stamp this Platform's clock for a pass cut short.
            result["stopped_reason"] = "stopped"
            break
        if batch and not dry_run:
            try:
                # delete-on-verify needs its snapshot up front, or
                # `_process_job` refuses to delete ("Delete plan snapshot
                # missing") and fails every job after doing the full
                # conversion. Same helper the manual path uses.
                snapshots = None
                unreadable: list[str] = []
                if rule["delete_on_verify"]:
                    snapshots = {}
                    for path in batch:
                        # One probe per Source, each up to its bound: a stop
                        # must not wait for the rest of them.
                        if stopping():
                            break
                        # Bounded, and a missing snapshot is not silently
                        # accepted: `_process_job` refuses to delete without
                        # one, so an unreadable source must drop out of the
                        # batch rather than reach the runner and fail there.
                        snapshot = await _probe(
                            build_delete_snapshot, path, default=None,
                        )
                        if snapshot is None:
                            unreadable.append(path)
                            continue
                        snapshots[path] = snapshot
                    if unreadable:
                        batch = [p for p in batch if p not in set(unreadable)]
                        logger.warning(
                            "romm_auto: dropping %d source(s) whose delete "
                            "snapshot could not be read", len(unreadable),
                        )
                    if not batch:
                        # Every candidate dropped, which is a symptom of the
                        # volume rather than of the library: queueing nothing
                        # would otherwise look like a clean run and advance
                        # this platform's schedule, postponing the whole set
                        # for a full interval over a transient failure.
                        logger.warning(
                            "romm_auto: platform %s had no readable source left "
                            "to queue", platform_id,
                        )
                        summary["snapshot_failed"] = True
                        result["errors"].append({
                            "platform_id": platform_ref,
                            "error": "sources_unreadable",
                        })
                        result["platforms"].append(summary)
                        continue

                if stopping():
                    # Arrived while the snapshots were read: queue none of
                    # this batch, and leave the Platform's clock alone.
                    result["stopped_reason"] = "stopped"
                    break
                owner = notifications.sweep_owner()
                jobs = await job_manager.create_batch_jobs(
                    batch,
                    ConversionMode(rule["mode"]),
                    output_dir=rule["output_dir"],
                    compression=_compression_arg(rule),
                    delete_on_verify=rule["delete_on_verify"],
                    # The Rule's check level, as the same opt-out a manual
                    # convert takes, so `_delete_check` decides both alike.
                    container_only=takes_container_only(rule),
                    delete_snapshots=snapshots,
                    split=rule["split"],
                    # delete_on_verify already verifies as a precondition, so
                    # asking for both would not verify twice -- but a rule that
                    # only wants the check must still get it.
                    verify_after=rule["verify_after"],
                    # The duplicate policy was resolved per candidate above;
                    # hand the answer down rather than let the queue derive a
                    # second, possibly different one.
                    output_paths=destinations,
                    allow_overwrite=rule["duplicate_action"] == "overwrite",
                    owner=owner,
                )
                summary["queued"] = len(jobs)
                result["queued"] += len(jobs)
                # The Library's Platform for each job, for its Ledger row
                # (#346). Before any await, so a job that starts at once has it.
                for job in jobs:
                    queued_image = image_by_path.get(job.file_path)
                    if queued_image is not None and queued_image.platform:
                        job.platform = queued_image.platform

                # Before the first await after acceptance. The terminal-job
                # listener fires from the queue worker, so a fast conversion on
                # an idle queue can end while the bookkeeping below is still
                # running -- and an outcome announced before its owner is
                # registered is discarded. See `_own_jobs`.
                job_by_path = {j.file_path: j.id for j in jobs}
                converted = _converted_entries([
                    (image_by_path[path].key, destinations[path],
                     pre_by_path[path], job_by_path.get(path))
                    for path in batch
                ])
                _own_jobs(
                    library, platform_id, converted, _output_identity(rule),
                    fingerprints={
                        str(image_by_path[path].key): fingerprint_by_path.get(path)
                        for path in batch
                    },
                    decision=(
                        _delete_decision_identity(rule, library),
                        _decision_generation(states[kind].get(platform_id)),
                    ),
                )
                notifications.sweep_jobs_queued(jobs)  # sync, before any await

                # Only now, with the jobs actually queued: the steps only this
                # Library performs (RomM snapshots metadata for formats it
                # cannot hash-match). Before the queue call they would leave
                # records behind for conversions that never ran.
                #
                # Every path in `batch` is queued or none is: create_jobs_atomic
                # takes the queue lock once and checks backpressure for the
                # whole set, so reaching here means the batch went in.
                await library.after_queue(
                    rule, spec,
                    [
                        QueuedImage(
                            image=image_by_path[path], source=path,
                            destination=destinations[path], pre=pre_by_path[path],
                        )
                        for path in batch
                    ],
                    summary, result,
                )

                # Record what this rule has now converted, so `overwrite` and
                # `rename` stop here instead of reconverting the same sources
                # every interval. Only after the queue accepted them.
                if rule["duplicate_action"] != "skip":
                    # Paired by source path, not by position (see
                    # `_converted_entries` above): the record is only
                    # meaningful if it names the job whose outcome decides
                    # whether this ROM was really converted.
                    try:
                        await _mark_converted(library, platform_id, converted)
                    except Exception:
                        # Isolated for the same reason as the re-pin write
                        # above, and this one is worse: the batch is already
                        # running, and reaching the handler below would report
                        # it as `queue_failed` -- which skips this very record
                        # and leaves `overwrite`/`rename` with no memory of the
                        # accepted jobs, so the next sweep converts them all
                        # again. A failed write costs this run its provenance
                        # and nothing else; the terminal-job listener still
                        # writes each outcome as it lands.
                        logger.warning(
                            "romm_auto: could not record what platform %s "
                            "produced; this run is not remembered",
                            platform_id, exc_info=True,
                        )
                        summary["provenance_failed"] = True

                # Claim them immediately so a later platform in the same sweep
                # cannot queue the same source twice. Already resolved during
                # selection, so this costs no further disk work.
                active_paths.update(resolved_by_path.values())
            except QueueBackpressureError:
                # The queue is full. Stop rather than spin: the next sweep
                # resumes exactly here, because the filesystem still reports
                # these ROMs unconverted.
                logger.info("romm_auto: queue full, stopping sweep")
                result["stopped_reason"] = "queue_full"
                # What this Platform's refused batch held, so a caller can say
                # how much work was left (run-rules).
                summary["unqueued"] = len(batch)
                result["platforms"].append(summary)
                break
            except OutputClaimedError as exc:
                # Someone else took a destination between this sweep planning
                # it and the queue accepting it -- a manual submit, or a
                # conversion started from another tab. Not an error in the
                # rule: the batch was refused whole, nothing was queued, and
                # the next sweep re-plans against the destination that now
                # exists. Named separately so the editor can say so instead of
                # showing "could not be queued".
                logger.info(
                    "romm_auto: platform %s lost a destination to another job: %s",
                    platform_id, exc,
                )
                result["errors"].append(
                    {"platform_id": platform_ref, "error": "destination_claimed"},
                )
                summary["queue_failed"] = True
            except Exception:
                logger.warning(
                    "romm_auto: failed to queue platform %s", platform_id, exc_info=True,
                )
                result["errors"].append(
                    {"platform_id": platform_ref, "error": "queue_failed"},
                )
                summary["queue_failed"] = True
        elif batch:
            summary["queued"] = len(batch)
            result["queued"] += len(batch)

        if unresponsive:
            summary["library_unresponsive"] = True
        summary["candidates"] = [Path(p).name for p in batch[:25]]
        result["platforms"].append(summary)
        # A real run updates the schedule clock; a preview must not, or looking
        # at what *would* happen would postpone the run that should. Nor does a
        # run that failed to queue anything: advancing the clock there would
        # make the platform sit out a full interval over a transient error.
        # Nor does one whose listing went out of date (`_record_run`).
        if not dry_run and not summary.get("queue_failed") and not unresponsive:
            await _record_run(library, platform_id, summary, listed_at=listed_at)

    if stopping():
        # A stop starts nothing new: what the check Rules picked is dropped and
        # their runs are not recorded, so the next sweep picks them again.
        result["stopped_reason"] = "stopped"
    elif not dry_run:
        async def record_check_runs(attempted: frozenset[str] | None = None) -> None:
            # Stamped with the moment the sweep found each Rule due, not when
            # the run finished: a night's run that ends past midnight still
            # counts for the window that picked it, so the next one keeps its
            # own run (4577171597). The reservation covers the time between.
            # With *attempted*, only a Rule whose picked Images the run all
            # reached is recorded: one it never got to (a stop on an abandoned
            # verifier, a cancel) stays due (4578054443). Each carries the walk
            # generation its pick listed at, compared now, as it is recorded:
            # conversions that finished while the job checked leave it due (#360).
            for platform_id, summary, manual, at, keys, listed_at in check_runs:
                if attempted is not None and not keys <= attempted:
                    continue
                await _record_run(
                    library, platform_id, summary, CHECK,
                    manual=manual, at=at, listed_at=listed_at,
                )

        # The scheduled runs this job carries hold their windows until it ends.
        reserved = {
            (library.id, str(platform_id))
            for platform_id, _summary, manual, _at, _keys, _listed in check_runs if not manual
        }

        async def check_run_ended(attempted: frozenset[str]) -> None:
            try:
                # From the check run, after this sweep let go of the lock:
                # taken again so the write cannot interleave with another
                # sweep's.
                async with _sweep_lock:
                    await record_check_runs(attempted)
            finally:
                # Recorded or not: a Rule the run did not finish stays due.
                _active_check_runs.difference_update(reserved)

        if picked:
            # One job and, if anything fails, one notification for the sweep.
            # The check Rules' runs are recorded once it has checked them all,
            # so a check cut short (a stop, a restart) leaves its Rule due.
            _active_check_runs.update(reserved)
            checks.start(
                library, picked, generation=_generations.get(library.id, 0),
                on_finished=check_run_ended,
            )
        else:
            await record_check_runs()
    result.update(await library.sweep_extras())
    return result


async def _clear_vanished(library: Library) -> None:
    """Clear *library*'s "not Intact" Findings whose file is gone (#305).

    A converted or deleted Image is never listed or checked again, so nothing
    else would. Bounded and detached: it stats each such Finding's path.
    """
    generation = _generations.get(library.id, 0)
    try:
        await asyncio.wait_for(
            run_detached(
                findings.clear_gone, library.id, findings.NOT_INTACT,
                current=lambda: _generations.get(library.id, 0) == generation,
            ),
            _CANDIDATE_PROBE_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm_auto: checking %s's not-Intact Findings did not finish; "
            "the next sweep tries again", library.id,
        )


def forget_checks_locked(library: Library) -> None:
    """Drop every record kept for *library*'s Images. Blocking.

    For a Library whose Image keys now name other Images (RomM pointed at
    another instance reuses ROM ids): its check times, every Finding (the
    "not Intact", Source-kept and duplicate ones) with its dismissals, and its
    last find-duplicates run, through the same call a folder Library's removal
    makes (:func:`services.library_records.forget`). The caller holds
    :func:`paused`; the Library's generation moves first, so a check or
    find-duplicates run in flight writes nothing more.
    """
    from services import library_records

    _generations[library.id] = _generations.get(library.id, 0) + 1
    library_records.forget(library.id)


def _walk_generation(library: Library) -> int:
    """*library*'s ``walk_generation()``; 0 for one without the optional hook.

    ``LibraryDefaults`` gives every Library class the hook. One that does not
    inherit it is read as listing live every time, whose listing never goes
    out of date.
    """
    hook = getattr(library, "walk_generation", None)
    return hook() if hook is not None else 0


def _listing_current(library: Library, seen: int) -> bool:
    """Whether *library*'s walk is still the one listed when it read *seen* (#353).

    A run is recorded (its schedule clock moves) only when this holds: a job
    that changed the Library's files after the listing (a conversion that
    added an Image) leaves the run looking at Images that are out of date,
    and recording it would make the new ones wait a full interval. Such a
    run is left due, and the next tick lists again. The same rule for every
    Rule; ``Library.walk_generation`` is where a Library says it moved.
    """
    return _walk_generation(library) == seen


async def _list_images(
    library: Library, platform_id: str, result: dict,
    stopping: Callable[[], bool] = lambda: False,
) -> list[Image] | None:
    """One Platform's Images, or None with the reason added to *result*.

    A Library that stops answering sets ``stopped_reason`` to
    ``library_unresponsive``, and a stop that arrives while it lists sets it
    to ``stopped``: either way the caller stops the sweep there
    (:func:`_sweep_ends`).
    """
    platform_ref = library.platform_value(platform_id)
    try:
        # Off the loop and bounded: the sweep holds `_sweep_lock` for the
        # whole call, and a Library on a mount that stops answering must
        # fail this sweep, not hold the lock until a restart. On a detached
        # daemon thread (process-wide cap), never the shared pool: an
        # abandoned listing would otherwise keep a pooled worker that
        # conversions need. Not `run_in_threadpool` either: anyio waits for
        # the thread when its caller is cancelled. Called through the
        # module so it is not the candidate probes' `run_detached`.
        # And stop-aware: a listing can take the whole bound, and a stop
        # (run-rules on SIGTERM) must not wait for it.
        images = await _wait_unless_stopped(
            subprocess_runner.run_detached(library.images, platform_id),
            _LISTING_SECONDS, stopping,
        )
    except _SweepStopped:
        result["stopped_reason"] = "stopped"
        return None
    except LibraryUnreadable as exc:
        if _unreadable.get((library.id, platform_id)) != exc.code:
            logger.warning(
                "romm_auto: platform %s of %s unreadable: %s",
                platform_id, library.id, exc,
            )
        _unreadable[(library.id, platform_id)] = exc.code
        # The Library's reason, so the summary can say "still running" or
        # "the folder is not reachable" rather than one generic failure.
        result["errors"].append({"platform_id": platform_ref, "error": exc.code})
        return None
    except asyncio.TimeoutError:
        _unreadable[(library.id, platform_id)] = "library_unresponsive"
        logger.warning(
            "romm_auto: listing platform %s did not finish in %ss; stopping "
            "the sweep (the Library is not answering)", platform_id,
            _LISTING_SECONDS,
        )
        result["errors"].append(
            {"platform_id": platform_ref, "error": "library_unresponsive"},
        )
        result["stopped_reason"] = "library_unresponsive"
        return None
    if _unreadable.pop((library.id, platform_id), None) is not None:
        logger.info(
            "romm_auto: platform %s of %s is readable again", platform_id, library.id,
        )
    return images


def _sweep_ends(result: dict) -> bool:
    """Whether a failed listing ends the whole sweep: the Library stopped
    answering, or the caller asked to stop."""
    return result["stopped_reason"] in ("library_unresponsive", "stopped")


async def _sweep_check(
    library: Library, platform_id: str, rule: dict, result: dict,
    active_paths: set[str], *, picked: list, check_runs: list, dry_run: bool,
    manual: bool = False, now: datetime | None = None,
    stopping: Callable[[], bool] = lambda: False,
) -> bool:
    """One check Rule's Platform: pick the stalest Images within its budget (#305).

    Adds the Platform's summary to *result*, what it picked to *picked* and
    the run to record to *check_runs*: the caller starts the job and records
    the runs once the pass is over, and neither if a stop came first.
    Returns True when the sweep must end (the Library stopped answering, or
    a stop). *manual* (Run now) records the run without moving the clock the
    schedule reads, so the nightly run still comes when it is due.
    """
    platform_ref = library.platform_value(platform_id)
    listed_at = _walk_generation(library)
    images = await _list_images(library, platform_id, result, stopping)
    if images is None:
        return _sweep_ends(result)
    budget = int(rule["check_budget_gb"]) * checks.GB
    last = await checks.order_stamps(library.id)
    ready: dict[str, bool] = {}
    chosen: list[checks.Check] = []
    spent = considered = uncheckable = held_elsewhere = 0
    unresponsive = not_ready = False
    for image in checks.stalest_first(images, last):
        if stopping():
            # Nothing of this Platform is picked or recorded.
            result["stopped_reason"] = "stopped"
            return True
        considered += 1
        if checks.in_flight(library.id, str(image.key)):
            # Held elsewhere: another check run (Run now) has it.
            result["skipped_active"] += 1
            held_elsewhere += 1
            continue
        try:
            # Detached and bounded, as a conversion candidate's probes are.
            decision = await asyncio.wait_for(
                run_detached(checks.inspect, image), _CANDIDATE_PROBE_SECONDS,
            )
        except (asyncio.TimeoutError, OSError):
            logger.warning(
                "romm_auto: platform %s library stopped responding, "
                "stopping this platform", platform_id,
            )
            result["errors"].append(
                {"platform_id": platform_ref, "error": "library_unresponsive"},
            )
            unresponsive = True
            break
        if "skip" in decision:
            if decision["skip"] == "missing":
                result["skipped_missing"] += 1
            elif decision["skip"] == "uncheckable":
                uncheckable += 1
            elif decision["skip"] == "busy":
                # Held elsewhere: a job (maybe in another process) writes it.
                result["skipped_active"] += 1
                held_elsewhere += 1
            continue
        if decision["resolved"] in active_paths:
            # Held elsewhere: queued or being converted; a later run checks it.
            result["skipped_active"] += 1
            held_elsewhere += 1
            continue
        item: checks.Check = decision["check"]
        if item.tool_id not in ready:
            # Whether its verifier can run here, which is not the same as
            # converting: jwud verifies in pure Python without JWUDTool, and
            # z3ds needs zstd to verify whatever converting needs (#306).
            ready[item.tool_id] = await registry.tool_is_ready(
                registry.get(item.tool_id), purpose="verify",
            )
        if not ready[item.tool_id]:
            # No verdict can come from a tool that is not installed: skipped,
            # never a "not Intact" Finding.
            not_ready = True
            uncheckable += 1
            continue
        # Stalest first, and never past the budget, except that one Image
        # larger than the whole budget is still checked rather than never.
        if chosen and spent + item.size > budget:
            break
        chosen.append(item)
        spent += item.size

    if not_ready:
        result["errors"].append({"platform_id": platform_ref, "error": "tool_not_ready"})
    result["considered"] += considered
    # Images a check Rule picked. Not conversions, so never in `queued` and
    # never against the per-sweep cap. Present only once a check Rule ran,
    # so a sweep of conversion Rules keeps its shape.
    result["checks"] = result.get("checks", 0) + len(chosen)
    summary = {
        "platform_id": platform_ref,
        "mode": "",
        "check": True,
        "queued": 0,
        **library.summary_fields(),
        "considered": considered,
        "checks": len(chosen),
        "check_bytes": spent,
        "budget_bytes": budget,
        "skipped_uncheckable": uncheckable,
        "candidates": [c.name for c in chosen[:25]],
    }
    if unresponsive:
        summary["library_unresponsive"] = True
    result["platforms"].append(summary)
    if not dry_run:
        picked.extend(chosen)
        # A Platform whose tool is missing waits for the next tick, not a day.
        # Nor is a pick that came up empty only because its Images are held
        # elsewhere for now (another check run, a conversion queued or running,
        # a writer's lock) the night's run (4578116078, 4578154650): once they
        # are free, the Rule must still be due. *listed_at* goes with the run:
        # a listing a finished job made out of date, before the pick ended or
        # while the job checks, leaves it unrecorded (`_record_run`, #353,
        # #360), though what was picked is still checked.
        if not unresponsive and not (not chosen and (not_ready or held_elsewhere)):
            check_runs.append((
                platform_id, summary, manual, now,
                frozenset(c.key for c in chosen), listed_at,
            ))
    return False


async def run_forever(libraries: Callable[[], Iterable[Library]]) -> None:
    """Background scheduler. Ticks once a minute; each platform runs on its own
    cadence, so per-platform intervals and windows are honoured without a
    timer per rule.

    *libraries* is asked on every tick, so a Library added or removed while
    the app runs is picked up without a restart."""
    logger.info("romm_auto: scheduler started")
    while True:
        await asyncio.sleep(60)
        try:
            due = list(libraries())
        except Exception:
            logger.warning("romm_auto: could not list the Libraries", exc_info=True)
            continue
        for library in due:
            try:
                # Re-read every tick: the master switch is editable in the app,
                # so turning it off must take effect without a restart.
                if not library.schedule_enabled():
                    continue
                summary = await sweep(library)
                if summary["queued"]:
                    logger.info(
                        "romm_auto: queued %s job(s) across %s platform(s)",
                        summary["queued"], len(summary["platforms"]),
                    )
            except Exception:
                # A sweep failure must never kill the scheduler, nor stop the
                # other Libraries' sweeps; the next tick retries. Shutdown
                # still stops it: CancelledError derives from BaseException,
                # so it passes straight through this handler.
                logger.warning("romm_auto: sweep failed", exc_info=True)
