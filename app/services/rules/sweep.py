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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from models import ConversionMode, JobStatus
from services import subprocess_runner
from services.image_sets import image_fingerprint
from services.job_manager import (
    OutputClaimedError,
    QueueBackpressureError,
    job_manager,
)
from services.output_conflicts import (
    QUEUE,
    SKIP_EXISTING,
    SKIP_LOCKED,
    resolve_destination,
)
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
)

# The logger name the engine has always logged under (see `rules.logger`).
logger = get_logger("romm_auto")

# How long one candidate's disk probes may take before the library counts as
# unresponsive. Generous for a healthy mount (these are a resolve and two
# stats) and short enough that a dead one costs a sweep rather than a restart.
_CANDIDATE_PROBE_SECONDS = 30
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

# One sweep at a time, process-wide. The active-source snapshot is taken before
# anything is queued, so the minute scheduler and a manual "Run now" could each
# read the same "nothing in flight" and queue the same ROM twice -- the job
# manager's creation lock serialises the inserts but does not deduplicate
# across two independently planned batches.
_sweep_lock = asyncio.Lock()

# job id -> (library, platform_id, image key) for the conversions this process
# queued. Read by `note_job_finished` so a completion updates its one record
# instead of scanning every remembered Image of every Platform. In memory on
# purpose: the job queue it indexes is in memory too, so an entry has nothing
# to outlive.
_job_owners: dict[str, tuple[Library, str, str]] = {}

# (library id, platform id) -> the reason it last could not be listed. A
# Platform that stays unreadable (a folder that is gone) is retried every tick;
# this logs the change of state, once each way, instead of every tick.
_unreadable: dict[tuple[str, str], str] = {}

# job id -> outcome, for a job that ended before its record was written. Drained
# by `_mark_converted`; see there. Bounded by the same thing that bounds
# `_job_owners`: one entry per queued conversion, removed when it is applied.
_late_verdicts: dict[str, bool] = {}


async def get_rules(library: Library) -> dict[str, dict]:
    """*library*'s stored rules, normalized without touching the filesystem.

    `check_volumes=False`: the output directory was validated when it was
    saved, and re-validating on every read would put a resolve plus a stat of
    every configured volume on the event loop -- in the rules API and in each
    sweep, which holds `_sweep_lock` while it runs. The sweep re-checks the
    destination off the loop before it queues anything, which is what covers a
    blob edited straight in the database.
    """
    stored = await store.read(store.RULES_KEY, library)
    return _normalize(library, stored, check_volumes=False)


def _normalize(library: Library, raw: Any, **kwargs) -> dict[str, dict]:
    """`normalize_rules` with *library*'s Platform ids and defaults."""
    return normalize_rules(
        raw,
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


async def normalize_rules_bounded(library: Library, raw: Any) -> dict[str, dict]:
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
            run_detached(functools.partial(_normalize, library, raw)),
            _VOLUME_PROBE_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "romm_auto: validating the rules' output directories did not finish "
            "in %ss; pausing the rules that name one (a volume is not answering)",
            _VOLUME_PROBE_SECONDS,
        )
        return _normalize(library, raw, volumes_unreachable=True)


async def set_rules(library: Library, raw: Any, *, strict: bool = False) -> dict[str, dict]:
    """Replace *library*'s rule set, forgetting provenance the new rules invalidate.

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
            await forget_converted_locked(library, stale)
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
) -> int:
    """:func:`forget_converted` for a caller already inside :func:`paused`."""
    state = await get_state(library)
    targets = (
        [str(p) for p in platform_ids] if platform_ids is not None else list(state)
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
    if cleared:
        await store.write(store.STATE_KEY, library, state)
    return cleared


async def forget_library(library: Library) -> None:
    """Drop every Rule and all run state *library* has. Touches no files.

    For a Library being removed. Under the sweep lock, so a sweep of it that is
    running finishes first and cannot write its state back afterwards, and the
    owner index loses the Library's queued jobs so their outcomes are not
    written back either.
    """
    async with _sweep_lock:
        for job_id, owner in list(_job_owners.items()):
            if owner[0].id == library.id:
                _job_owners.pop(job_id, None)
        for key in [k for k in _unreadable if k[0] == library.id]:
            _unreadable.pop(key, None)
        await store.write(store.RULES_KEY, library, {})
        await store.write(store.STATE_KEY, library, {})


async def get_state(library: Library) -> dict[str, dict]:
    """*library*'s run state: schedule stamps and converted history, by Platform."""
    return await store.read(store.STATE_KEY, library)


async def _record_run(library: Library, platform_id: str, summary: dict) -> None:
    """Stamp the schedule clock, merging into whatever else the entry holds.

    The per-platform entry is shared with ``converted_ids``: replacing it
    wholesale would forget, on every single run, which sources the rule had
    already converted -- and that set is the only thing stopping an
    ``overwrite`` rule from reconverting the library forever.
    """
    state = await get_state(library)
    entry = dict(state.get(str(platform_id)) or {})
    entry.update({
        "last_run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_queued": summary.get("queued", 0),
        "last_considered": summary.get("considered", 0),
    })
    state[str(platform_id)] = entry
    await store.write(store.STATE_KEY, library, state)


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
    so sending the codec alone silently dropped the level the operator set.
    """
    codec = rule["compression"]
    level = rule["compression_level"]
    if level is None:
        return codec or None
    if not codec and registry.spec(rule["mode"]).supports_compression:
        # An empty codec is only a "tool default" where the mode offers no
        # codec choice at all: nsz reads the empty layout part of ":18" and
        # still honours the level. Where the mode *does* have a codec picker
        # and the operator left it on "Tool default", the empty part is a
        # blank, not a default -- dolphin-tool receives `-c "" -l 19` and
        # every such job fails at the tool -- so name the tool's own default.
        codec = registry.default_compression(rule["mode"])
        if not codec:
            # A codec-picking mode whose tool declares no default. The level
            # cannot be expressed without a codec, and inventing one would
            # convert the library with settings the operator did not choose,
            # so drop the level and say so rather than queue a failing job.
            logger.warning(
                "romm_auto: %s needs a codec alongside a compression level and "
                "declares no default; ignoring the level",
                rule["mode"],
            )
            return None
    # A level with no codec is meaningful: nsz reads the empty layout part as
    # "tool default" and still honours the level, so ":18" must not collapse to
    # None or the operator's level would be silently discarded.
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
    if owner is None:
        return
    library, platform_id, rom_id = owner
    done = status == JobStatus.COMPLETED
    if FAILED_HOLD in library.capabilities and status != JobStatus.CANCELLED:
        await _note_failure(library, platform_id, rom_id, job, failed=not done)
    if CONVERTED_HISTORY not in library.capabilities:
        # No converted history to write the verdict into (skip-only Rules).
        return
    async with _sweep_lock:
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
) -> None:
    """Hold a failed Image until its file changes, or release a converted one.

    Only for a Library with :data:`FAILED_HOLD`. The hold is the Source's
    fingerprint (size and mtime) when the job failed; the sweep compares it
    with the file's now. A cancelled job is neither: it says nothing about
    the Image.
    """
    fingerprint = None
    if failed:
        source = getattr(job, "file_path", None)
        if not source:
            return
        fingerprint = await _probe(image_fingerprint, source, default="")
        if not fingerprint:
            return
    async with _sweep_lock:
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


def _own_jobs(library: Library, platform_id: str, entries: dict) -> None:
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
            _job_owners[str(record["job_id"])] = (
                library, str(platform_id), str(rom_id),
            )


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
    failed: dict | None = None,
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
    """
    miss = {
        "skip": "unresolvable", "resolved": None, "destination": None,
        "path": None, "pre": None,
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
    if failed and failed.get("fingerprint") == image_fingerprint(path):
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
    }


def _active_source_paths() -> set[str]:
    """Sources already queued or converting, resolved once per sweep."""
    out: set[str] = set()
    for _job_id, paths in job_manager.get_active_job_candidates():
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
) -> dict:
    """Run one pass over *library*'s ruled Platforms.

    Returns a summary rather than raising on a partial failure: one unreachable
    platform must not abort the sweep for the others.

    Serialised process-wide, so a scheduled tick and a manual run cannot both
    plan against the same "nothing is in flight" snapshot. A preview is a real
    sweep in every respect but the queueing, so it takes the lock too rather
    than reporting candidates a concurrent run is already claiming.
    """
    async with _sweep_lock:
        blocked = library.sweep_blocked()
        if blocked:
            return {
                "queued": 0, "considered": 0, "platforms": [],
                "errors": [{"platform_id": None, "error": blocked}],
                **await library.sweep_extras(),
            }
        result = await _sweep_locked(
            library,
            platform_ids=platform_ids,
            ignore_schedule=ignore_schedule,
            dry_run=dry_run,
            overall_limit=overall_limit,
        )
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
) -> dict:
    """The sweep body. Always called with ``_sweep_lock`` held."""
    rules = await get_rules(library)
    state = await get_state(library)
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
    if not rules:
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

    # Priority first (lower runs earlier), then id, so the order is total and
    # a capped sweep resumes where the last one stopped.
    ordered = sorted(
        rules, key=lambda pid: (rules[pid]["priority"], library.platform_value(pid)),
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

    for platform_id in ordered:
        if result["queued"] >= cap:
            result["stopped_reason"] = "limit"
            break
        rule = rules[platform_id]
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
        if not rule["enabled"] and (wanted is None or _validation_paused(rule)):
            if wanted is not None:
                result["errors"].append(
                    {"platform_id": platform_ref, "error": "rule_invalid"},
                )
            continue
        # ignore_schedule bypasses only the clock (interval, window, weekday).
        if not ignore_schedule and not _is_due(rule, state.get(platform_id, {}), now):
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

        try:
            # Off the loop and bounded: the sweep holds `_sweep_lock` for the
            # whole call, and a Library on a mount that stops answering must
            # fail this sweep, not hold the lock until a restart. On a detached
            # daemon thread (process-wide cap), never the shared pool: an
            # abandoned listing would otherwise keep a pooled worker that
            # conversions need. Not `run_in_threadpool` either: anyio waits for
            # the thread when its caller is cancelled. Called through the
            # module so it is not the candidate probes' `run_detached`.
            images = await asyncio.wait_for(
                subprocess_runner.run_detached(library.images, platform_id),
                _LISTING_SECONDS,
            )
        except LibraryUnreadable as exc:
            if _unreadable.get((library.id, platform_id)) != exc.code:
                logger.warning(
                    "romm_auto: platform %s of %s unreadable: %s",
                    platform_id, library.id, exc,
                )
            _unreadable[(library.id, platform_id)] = exc.code
            # The Library's reason, so the summary can say "still running" or
            # "the folder is not reachable" rather than one generic failure.
            result["errors"].append(
                {"platform_id": platform_ref, "error": exc.code},
            )
            continue
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
            break
        if _unreadable.pop((library.id, platform_id), None) is not None:
            logger.info(
                "romm_auto: platform %s of %s is readable again", platform_id, library.id,
            )

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
        batch: list[str] = []
        unresponsive = False
        image_by_path: dict[str, Image] = {}
        destinations: dict[str, str] = {}
        resolved_by_path: dict[str, str] = {}
        pre_by_path: dict[str, str] = {}
        considered = 0

        for image in sorted(images, key=_image_sort_key(rule)):
            if len(batch) >= per_platform_cap:
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
                        _inspect_candidate, image, rule, tool, spec,
                        converted.get(str(image.key)), held.get(str(image.key)),
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
        if rule["delete_on_verify"]:
            # What Preview shows: the check that will actually pass before
            # this batch deletes a Source, not the stored preference (a mode
            # with no digest deletes on Intact). Absent when nothing is deleted.
            summary["check_level"] = effective_check_level(rule)

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
                )
                summary["queued"] = len(jobs)
                result["queued"] += len(jobs)

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
                _own_jobs(library, platform_id, converted)

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
        if not dry_run and not summary.get("queue_failed") and not unresponsive:
            await _record_run(library, platform_id, summary)

    result.update(await library.sweep_extras())
    return result


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
