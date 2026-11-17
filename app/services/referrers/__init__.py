"""Referrer registry and the one hook the job pipeline calls (#295).

Referrers are registered here, like tools in ``services.tools``. Callers never
branch on a Referrer's identity: they ask for every Referrer's planned change
(``plan_changes``, used by the delete confirmation) or run them all
(``update_before_source_removal``, used by the job before it deletes a Source).
Both are given the Image's Library (``libraries.library_for``; None when the
Image is in none), and the registry decides which Referrers are on for it
(``ReferrerRegistry.active``): every always-on one, and each opt-in one whose
Library's settings turn it on (#383).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from logging_setup import get_logger
from services.subprocess_runner import (
    ReadCancelled,
    conversion_stall_bound,
    run_detached,
    run_detached_within_stall_bound,
)
from utils.path_utils import file_identity

from .base import (
    Applied,
    Phase,
    Referrer,
    ReferrerChange,
    ReferrerError,
    StagedPlacement,
    WriteGuard,
    atomic_write_bytes,
    guarded_context,
    write_guard,
    write_new_bytes,
)
from .gamelist import GamelistReferrer
from .m3u import M3uReferrer

logger = get_logger("referrers")

# What a job says when it keeps a Source because which Library holds the Image
# can't be told (#413).
UNDECIDED_LIBRARY_KEPT = "couldn't tell which Library holds it"


@dataclass(frozen=True)
class UndecidedLibrary:
    """Stands in for the Image's Library when which one holds it can't be told (#413).

    The caller that looked the Library up (``libraries.library_for`` raised
    ``AmbiguousLibrary``: the file is listed in one Library and lives in
    another) passes this instead of a Library. :meth:`ReferrerRegistry.active`
    raises ReferrerError for it, so the hook keeps the Source with *reason*,
    the delete confirmation says so, and ``places_output`` answers False.
    """

    target: str
    reason: str


class ReferrerRegistry:
    def __init__(self) -> None:
        self._referrers: dict[str, Referrer] = {}

    def register(self, referrer: Referrer) -> None:
        if referrer.id in self._referrers:
            raise ValueError(f"Referrer {referrer.id!r} is already registered")
        if not isinstance(getattr(referrer, "phase", None), Phase):
            raise TypeError(f"Referrer {referrer.id!r} must declare its phase")
        if not isinstance(getattr(referrer, "opt_in", None), bool):
            raise TypeError(f"Referrer {referrer.id!r} must declare whether it is opt-in")
        self._referrers[referrer.id] = referrer

    def all(self) -> list[Referrer]:
        return list(self._referrers.values())

    def active(self, library) -> list[Referrer]:
        """The Referrers that are on for an Image in *library*, in phase order.

        Every always-on Referrer, and each opt-in one whose Library's settings
        turn it on (``library.referrer_enabled``). An Image in no Library
        (*library* None) gets the always-on ones only. Registration order is
        kept within a phase. Each is bound to *library*
        (``Referrer.for_library``, #387). Raises ReferrerError (target: the
        Library) when the settings can't be read, so the caller keeps the Source,
        and for an :class:`UndecidedLibrary` (#413).
        """
        if isinstance(library, UndecidedLibrary):
            raise ReferrerError(library.target, library.reason, kept=UNDECIDED_LIBRARY_KEPT)
        return [
            self._bound(referrer, library)
            for referrer in sorted(self._referrers.values(), key=lambda r: r.phase)
            if not referrer.opt_in or self._enabled(referrer, library)
        ]

    @staticmethod
    def _bound(referrer: Referrer, library) -> Referrer:
        try:
            return referrer.for_library(library)
        except ReferrerError:
            raise
        except Exception as exc:
            # Not knowing its settings must keep the Source: broad on purpose.
            raise ReferrerError(
                getattr(library, "name", None) or getattr(library, "id", "Library"),
                f"couldn't read the settings of {referrer.label} ({exc})",
            ) from exc

    @staticmethod
    def _enabled(referrer: Referrer, library) -> bool:
        if library is None:
            return False
        try:
            return library.referrer_enabled(referrer.id) is True
        except Exception as exc:
            # Not knowing whether it's on must keep the Source: broad on purpose.
            raise ReferrerError(
                getattr(library, "name", None) or getattr(library, "id", "Library"),
                f"couldn't read whether {referrer.label} is on ({exc})",
            ) from exc


registry = ReferrerRegistry()
registry.register(M3uReferrer())
registry.register(GamelistReferrer())


def _applies(referrer: Referrer, image_path: str, renames: Mapping[str, str]) -> bool:
    """Whether *referrer* could point at the Image or any file being replaced.

    Every deleted path is asked, not just the Image's own: a cue's track in a
    subfolder can have a playlist of its own.
    """
    return any(referrer.detect(p) for p in dict.fromkeys([image_path, *renames]))


def places_output(image_path: str, renames: Mapping[str, str], library=None) -> bool:
    """Whether a Referrer on for *library* moves the staged output into place
    itself (``Referrer.places_output``), so the job must keep its output hidden
    until the hook runs and hand the hook a :class:`StagedPlacement` (#391).
    Cheap: reads settings in memory, no I/O. False when the Library's
    settings can't be read (the hook then keeps the Source anyway)."""
    try:
        active = registry.active(library)
    except ReferrerError:
        return False
    return any(referrer.places_output(image_path, renames) for referrer in active)


def plan_changes(
    image_path: str, renames: Mapping[str, str], library=None,
) -> list[ReferrerChange]:
    """The planned change of every Referrer on for *library*, for *renames*,
    in phase order. Writes nothing.

    Raises ReferrerError when a Referrer can't tell what it would change, or
    the Library's settings can't be read.
    """
    changes: list[ReferrerChange] = []
    for referrer in registry.active(library):
        if _applies(referrer, image_path, renames):
            changes.extend(referrer.plan(renames))
    return changes


@dataclass
class ReferrerOutcome:
    """What the hook did.

    ``records`` has one entry per Referrer target, in order:
    ``{"referrer", "target", "status": "updated" | "failed" | "skipped" |
    "abandoned"}``, plus ``"entries"`` (how many entries changed, #386) for
    an updated one and ``"reason"`` for all the others. It is what the
    Ledger's ``referrers_updated`` column stores. ``failed``/``reason`` name
    the first failure, which is why the Source was kept.

    ``lingering`` holds the async applies that were cancelled and were still
    cleaning up when the hook gave up on them ("abandoned"). The caller hands
    them its holds with :meth:`outlive` instead of letting go itself.

    ``removed`` lists the Source files a Referrer already removed itself
    (``Applied``, the RomM swap, #391): the caller records them as deleted
    and does not delete them again. ``kept`` is the failing Referrer's own
    wording for :attr:`kept_message` (``ReferrerError.kept``).
    """

    records: list[dict] = field(default_factory=list)
    failed: str | None = None
    reason: str | None = None
    kept: str | None = None
    removed: list[str] = field(default_factory=list)
    # What a Referrer that applied wants the job to say (``Applied.note``).
    notes: list[str] = field(default_factory=list)
    # The async applies a phase's thread left for the event loop, in order.
    pending: list[tuple[Referrer, ReferrerChange]] = field(
        default_factory=list, repr=False, compare=False,
    )
    # The phase's write guard, carried into its async applies' tasks.
    guard: WriteGuard | None = field(default=None, repr=False, compare=False)
    lingering: list[asyncio.Task] = field(default_factory=list, repr=False, compare=False)
    # The same applies with what each was doing, for the caller to take
    # their results when they end (Codex 4595004160).
    handed_off: list[HandedOff] = field(default_factory=list, repr=False, compare=False)

    def outlive(self, release: Callable[[], object]) -> None:
        """Call *release* once every lingering apply has ended: at once when
        none is lingering, else from the last one's done callback (on the
        loop, so *release* must not block). Like ``run_detached_mutation``'s
        hand-off: whoever runs last lets go."""
        remaining = set(self.lingering)
        if not remaining:
            release()
            return

        def ended(task: asyncio.Task) -> None:
            remaining.discard(task)
            if not remaining:
                release()

        for task in self.lingering:
            task.add_done_callback(ended)

    @property
    def ok(self) -> bool:
        return self.failed is None

    @property
    def updated(self) -> list[dict]:
        return [r for r in self.records if r["status"] == "updated"]

    @property
    def kept_message(self) -> str:
        if self.kept:
            return f"converted, source kept: {self.kept} ({self.reason})"
        name = os.path.basename(self.failed or "")
        return f"converted, source kept: couldn't update {name} ({self.reason})"

    def _record(
        self, referrer: str, target: str, status: str, reason: str | None = None,
        *, entries: int | None = None,
    ):
        record = {"referrer": referrer, "target": target, "status": status}
        if reason is not None:
            record["reason"] = reason
        if entries is not None:
            record["entries"] = entries
        self.records.append(record)

    def _updated(
        self, referrer: str, change: ReferrerChange, applied: object = None,
    ) -> None:
        self._record(referrer, change.target, "updated", entries=len(change.edits))
        # The change's own Ledger fields (the RomM swap's ROM id), never
        # overriding the ones every record has.
        for key, value in (change.record or {}).items():
            self.records[-1].setdefault(key, value)
        if isinstance(applied, Applied):
            self.removed.extend(applied.removed)
            if applied.note:
                self.records[-1].setdefault("note", applied.note)
                self.notes.append(applied.note)

    def _fail(self, referrer: str, exc: Exception, fallback_target: str) -> None:
        if isinstance(exc, ReferrerError):
            target, reason, kept = exc.target, exc.reason, exc.kept
        else:
            target, reason, kept = fallback_target, str(exc) or type(exc).__name__, None
        self._record(referrer, target, "failed", reason)
        self.failed, self.reason, self.kept = target, reason, kept


_SKIPPED = "not attempted after an earlier Referrer failed"


def _update_sync(
    image_path: str, renames: Mapping[str, str],
    abandoned: threading.Event | None = None,
    referrers: list[Referrer] | None = None,
    placement: StagedPlacement | None = None,
) -> ReferrerOutcome:
    """Plan *referrers* (one phase; by default every always-on Referrer), then
    apply each change. The first failure stops it.

    Every write is guarded (``base.check_write``, under the target's lock):
    once *abandoned* is set, or an output no longer has the identity it had
    when this started, nothing more is written. An output still staged
    (*placement*, #391) is watched where it is staged. A Referrer with its own
    ``apply_async`` is not applied here: it is left in ``outcome.pending`` for
    the event loop.
    """
    staged = placement.staged if placement is not None else {}
    watched = [staged.get(new, new) for new in dict.fromkeys(renames.values())]
    guard = WriteGuard(
        abandoned=abandoned or threading.Event(),
        outputs={path: file_identity(path) for path in watched},
        placement=placement,
    )
    if referrers is None:
        referrers = registry.active(None)
    with write_guard(guard):
        outcome = _plan_and_apply(image_path, renames, referrers)
    outcome.guard = guard
    return outcome


def _is_async(referrer: Referrer) -> bool:
    """Whether *referrer* is applied on the event loop (``apply_async``)."""
    return referrer.applies_async()


def _plan_and_apply(
    image_path: str, renames: Mapping[str, str], referrers: list[Referrer],
) -> ReferrerOutcome:
    outcome = ReferrerOutcome()
    planned: list[tuple[Referrer, ReferrerChange]] = []
    for referrer in referrers:
        try:
            if _applies(referrer, image_path, renames):
                planned.extend((referrer, c) for c in referrer.plan(renames))
        except Exception as exc:
            # Any failure keeps the Source, so the catch is deliberately broad.
            logger.warning(
                "Referrer %s could not plan for %s", referrer.id, image_path, exc_info=True,
            )
            outcome._fail(referrer.id, exc, referrer.id)
            break
    for referrer, change in planned:
        if not outcome.ok:
            outcome._record(referrer.id, change.target, "skipped", _SKIPPED)
            continue
        if _is_async(referrer):
            outcome.pending.append((referrer, change))
            continue
        try:
            applied = referrer.apply(change)
        except Exception as exc:
            # Any failure keeps the Source, so the catch is deliberately broad.
            logger.warning(
                "Referrer %s could not update %s", referrer.id, change.target, exc_info=True,
            )
            outcome._fail(referrer.id, exc, change.target)
            continue
        # What it returned (``Applied``) counts as an async apply's does.
        outcome._updated(referrer.id, change, applied)
    return outcome


def _by_phase(referrers: list[Referrer]) -> list[list[Referrer]]:
    """*referrers* (already in phase order) split into one list per phase."""
    groups: list[list[Referrer]] = []
    for referrer in referrers:
        if groups and groups[-1][0].phase == referrer.phase:
            groups[-1].append(referrer)
        else:
            groups.append([referrer])
    return groups


def _limit(bound: float | None) -> float | None:
    """The seconds an async apply may take: *bound*, else the conversion's
    no-progress bound. None (no bound) when that is switched off."""
    limit = conversion_stall_bound() if bound is None else bound
    return limit if limit > 0 else None


# How long a cancelled async apply gets to finish its cleanup before the hook
# stops waiting and hands it the caller's holds (codex 4588646155).
CANCEL_GRACE_SECONDS = 5.0


class _Abandoned(ReferrerError):
    """An async apply was cancelled and was still cleaning up after the grace."""

    def __init__(self, target: str, reason: str, task: asyncio.Task) -> None:
        super().__init__(target, reason)
        self.task = task


@dataclass(frozen=True)
class HandedOff:
    """An async apply still running when the hook's own awaiter was cancelled
    (a shutdown, #391): the caller hands it its holds and, when it ends,
    takes what it did (:meth:`outcome`)."""

    referrer_id: str
    change: ReferrerChange
    task: asyncio.Task

    def outcome(self) -> ReferrerOutcome | None:
        """What the ended apply did, as the hook would have recorded it, or
        None when it failed or was cancelled."""
        task = self.task
        if not task.done() or task.cancelled() or task.exception() is not None:
            return None
        outcome = ReferrerOutcome()
        outcome._updated(self.referrer_id, self.change, task.result())
        return outcome


# Applies nobody awaits any more, kept referenced until they end: the loop
# holds only weak references to tasks, and one collected mid-cleanup would
# never run its `finally` nor release the holds handed to it.
_unawaited: set[asyncio.Task] = set()


def _let_it_end(task: asyncio.Task, referrer_id: str, target: str) -> None:
    """Keep *task* alive until it ends, and log how it ended."""
    _unawaited.add(task)
    task.add_done_callback(_unawaited.discard)
    task.add_done_callback(_log_its_end(referrer_id, target))


def _log_its_end(referrer_id: str, target: str) -> Callable[[asyncio.Task], None]:
    """A done callback that logs how an apply nobody awaits any more ended."""

    def ended(task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        logger.warning(
            "Referrer %s: the abandoned update of %s ended with an error",
            referrer_id, target, exc_info=task.exception(),
        )

    return ended


async def _await_bounded(
    referrer: Referrer, change: ReferrerChange, *, guard: WriteGuard | None,
    cancel_event: asyncio.Event | None, bound: float | None,
    unawaited: list[HandedOff] | None = None,
) -> object:
    """Await an async apply on the loop, bounded and raced against Cancel,
    and return what it returned.

    The apply's task runs in a context holding *guard*, so its ``check_write``
    refuses as the thread's does. Past the bound or on a Cancel the apply is
    cancelled and given ``CANCEL_GRACE_SECONDS`` to finish its cleanup; then
    ReferrerError names the target. Still running after that, it raises
    :class:`_Abandoned` carrying the task, whose end is logged whenever it
    comes. A cancel of this awaiter cancels the apply and propagates.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ReferrerError(change.target, "cancelled")
    limit = _limit(bound)
    task = asyncio.get_running_loop().create_task(
        referrer.apply_async(change),
        context=guarded_context(guard) if guard is not None else None,
    )
    stop = asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
    try:
        done, _ = await asyncio.wait(
            {task} if stop is None else {task, stop},
            timeout=limit, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            return task.result()
        reason = (
            "cancelled" if cancel_event is not None and cancel_event.is_set()
            else f"did not answer within {limit:g}s"
        )
        task.cancel()
        finished, _ = await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        _let_it_end(task, referrer.id, change.target)
        if unawaited is not None:
            # This awaiter is cancelled (a shutdown) while the apply still
            # cleans up, or just after it finished: the caller hands it its
            # holds, as ``outlive`` does, and takes its result when it ends
            # (CodeRabbit 4594841860), at once for one already done, so a
            # result that beat the cancel is never lost (CodeRabbit
            # 4594978832).
            unawaited.append(HandedOff(referrer.id, change, task))
        raise
    finally:
        if stop is not None:
            stop.cancel()
    if task not in finished:
        _let_it_end(task, referrer.id, change.target)
        raise _Abandoned(
            change.target, f"{reason}; still cleaning up, so it was abandoned", task,
        )
    if not task.cancelled() and task.exception() is None:
        # It finished its work all the same (the RomM swap whose last step
        # had already removed the Source, #391): what it did counts.
        return task.result()
    _log_its_end(referrer.id, change.target)(task)
    raise ReferrerError(change.target, reason)


async def _apply_pending(
    outcome: ReferrerOutcome, pending: list[tuple[Referrer, ReferrerChange]], *,
    guard: WriteGuard | None, cancel_event: asyncio.Event | None, bound: float | None,
    unawaited: list[HandedOff] | None = None,
) -> None:
    """Apply a phase's async changes in order; the first failure stops it."""
    for referrer, change in pending:
        if not outcome.ok:
            outcome._record(referrer.id, change.target, "skipped", _SKIPPED)
            continue
        try:
            applied = await _await_bounded(
                referrer, change, guard=guard, cancel_event=cancel_event, bound=bound,
                unawaited=unawaited,
            )
        except _Abandoned as exc:
            logger.warning("Referrer %s: %s", referrer.id, exc)
            outcome._record(referrer.id, exc.target, "abandoned", exc.reason)
            outcome.failed, outcome.reason = exc.target, exc.reason
            outcome.lingering.append(exc.task)
            outcome.handed_off.append(HandedOff(referrer.id, change, exc.task))
            continue
        except Exception as exc:
            # Any failure keeps the Source, so the catch is deliberately broad.
            logger.warning(
                "Referrer %s could not update %s", referrer.id, change.target, exc_info=True,
            )
            outcome._fail(referrer.id, exc, change.target)
            continue
        outcome._updated(referrer.id, change, applied)


async def _run_phase(
    image_path: str, renames: dict[str, str], abandoned: threading.Event,
    referrers: list[Referrer], *, cancel_event, bound: float | None,
    placement: StagedPlacement | None = None,
) -> ReferrerOutcome:
    """One phase's plans and blocking applies, on a detached thread, bounded."""
    if bound is not None:
        # A caller with its own, tighter bound (a retried delete, #329).
        return await asyncio.wait_for(
            run_detached(
                _update_sync, image_path, renames, abandoned, referrers, placement,
                cancel_event=cancel_event,
            ),
            bound,
        )
    return await run_detached_within_stall_bound(
        _update_sync, image_path, renames, abandoned, referrers, placement,
        what=f"Updating {referrers[0].phase.target}", cancel_event=cancel_event,
    )


async def update_before_source_removal(
    image_path: str, renames: Mapping[str, str], *, library=None,
    cancel_event=None, bound: float | None = None,
    placement: StagedPlacement | None = None,
    unawaited: list[HandedOff] | None = None,
) -> ReferrerOutcome:
    """Run every Referrer that is on for an Image whose Source is about to be removed.

    *unawaited*, when given, receives each async apply still running when
    this awaiter itself is cancelled (a shutdown, #391): the caller hands it
    its holds (``ReferrerOutcome(lingering=...).outlive``), so they are let
    go only when it ends, as for an apply abandoned after the grace.

    *renames* maps each Source file that will be deleted to the output that
    replaces it. *library* is the Image's Library (None when it is in none);
    it decides which opt-in Referrers run. Referrers run phase by phase: a
    phase's plans and blocking applies on a detached thread, then its async
    applies on the loop. A later phase runs only when every earlier Referrer
    succeeded. Stops at the first failure: the caller must then keep the
    Source. Never raises.

    Bounded and cancellable like the job's other reads of a mount that may
    stop answering: each phase's thread runs under the conversion's
    no-progress bound (or *bound*), each async apply under the same bound,
    and *cancel_event* returns at once. On a timeout or a cancel the thread
    is abandoned (an async apply is cancelled) and the outcome keeps the
    Source. The abandoned thread is told so: it checks under each playlist's
    lock before every write and writes nothing more, so it can't point a
    playlist at an output another job has since replaced.

    *placement* is given when the job kept its output staged for a Referrer
    that places it (``places_output``, #391); every phase and apply sees it
    (``base.staged_placement``). Placing what is still staged afterwards is
    the caller's job.
    """
    outcome = ReferrerOutcome()
    try:
        phases = _by_phase(registry.active(library))
    except ReferrerError as exc:
        logger.warning("Referrer hook for %s: %s", image_path, exc)
        outcome._fail("library", exc, image_path)
        return outcome
    abandoned = threading.Event()
    try:
        await _run_phases(
            outcome, phases, image_path, dict(renames), abandoned,
            cancel_event=cancel_event, bound=bound, placement=placement,
            unawaited=unawaited,
        )
    except BaseException:
        # This awaiter was cancelled (or worse) mid-phase: the detached worker,
        # or an async apply's cleanup, may still run, and must write nothing
        # more once the caller lets go of its locks (CodeRabbit 4588773569).
        abandoned.set()
        raise
    if not outcome.ok:
        # Whatever still runs (an abandoned thread, an apply cleaning up)
        # writes nothing more.
        abandoned.set()
    return outcome


async def _run_phases(
    outcome: ReferrerOutcome, phases: list[list[Referrer]], image_path: str,
    renames: dict[str, str], abandoned: threading.Event, *,
    cancel_event, bound: float | None, placement: StagedPlacement | None = None,
    unawaited: list[HandedOff] | None = None,
) -> None:
    """The hook's phase loop, filling *outcome*. Raises only on a cancel of
    the awaiter, after setting *abandoned*."""
    for referrers in phases:
        if not outcome.ok:
            # A later phase never runs after an earlier one failed.
            for referrer in referrers:
                outcome._record(referrer.id, image_path, "skipped", _SKIPPED)
            continue
        try:
            stage = await _run_phase(
                image_path, renames, abandoned, referrers,
                cancel_event=cancel_event, bound=bound, placement=placement,
            )
        except ReadCancelled:
            reason = "cancelled"
            logger.warning("Referrer hook for %s did not finish: cancelled", image_path)
        except asyncio.CancelledError:
            # The awaiter is cancelled; the detached worker keeps running.
            abandoned.set()
            raise
        except Exception as exc:
            # A timeout or anything else keeps the Source, so the catch is broad.
            reason = str(exc) or type(exc).__name__
            logger.warning(
                "Referrer hook for %s did not finish", image_path, exc_info=True,
            )
        else:
            outcome.records.extend(stage.records)
            outcome.failed, outcome.reason = stage.failed, stage.reason
            outcome.kept = stage.kept
            # What the phase's plain applies removed or noted (``Applied``).
            outcome.removed.extend(stage.removed)
            outcome.notes.extend(stage.notes)
            await _apply_pending(
                outcome, stage.pending, guard=stage.guard,
                cancel_event=cancel_event, bound=bound, unawaited=unawaited,
            )
            continue
        abandoned.set()
        # One failed record per Referrer of the phase, so the Ledger row says
        # why the Source was kept even though no target was reached.
        for referrer in referrers:
            outcome._record(referrer.id, image_path, "failed", reason)
        outcome.failed, outcome.reason = referrers[0].phase.target, reason


async def plan_changes_bounded(
    image_path: str, renames: Mapping[str, str], library=None,
) -> list[ReferrerChange]:
    """``plan_changes`` for the delete confirmation, bounded like the job's
    hook: on a detached thread under the conversion's no-progress bound, so a
    playlist on a mount that stopped answering can't hang the confirmation.
    *library* is the Image's Library, as the job's hook gets it. Raises
    ReferrerError (target: the file phase's ``Phase.target``) on a timeout."""
    try:
        return await run_detached_within_stall_bound(
            plan_changes, image_path, dict(renames), library,
            what=f"Reading {Phase.FILE.target}",
        )
    except ReferrerError:
        raise
    except Exception as exc:
        raise ReferrerError(Phase.FILE.target, str(exc) or type(exc).__name__) from exc


__all__ = [
    "Applied",
    "HandedOff",
    "Phase",
    "Referrer",
    "ReferrerChange",
    "ReferrerError",
    "ReferrerOutcome",
    "ReferrerRegistry",
    "StagedPlacement",
    "UndecidedLibrary",
    "atomic_write_bytes",
    "places_output",
    "plan_changes",
    "plan_changes_bounded",
    "registry",
    "update_before_source_removal",
    "write_new_bytes",
]
