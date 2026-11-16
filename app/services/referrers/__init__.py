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
import inspect
import os
import threading
from collections.abc import Mapping
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
    Phase,
    Referrer,
    ReferrerChange,
    ReferrerError,
    WriteGuard,
    atomic_write_bytes,
    write_guard,
    write_new_bytes,
)
from .m3u import M3uReferrer

logger = get_logger("referrers")


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
        kept within a phase. Raises ReferrerError (target: the Library) when
        the settings can't be read, so the caller keeps the Source.
        """
        return [
            referrer
            for referrer in sorted(self._referrers.values(), key=lambda r: r.phase)
            if not referrer.opt_in or self._enabled(referrer, library)
        ]

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


def _applies(referrer: Referrer, image_path: str, renames: Mapping[str, str]) -> bool:
    """Whether *referrer* could point at the Image or any file being replaced.

    Every deleted path is asked, not just the Image's own: a cue's track in a
    subfolder can have a playlist of its own.
    """
    return any(referrer.detect(p) for p in dict.fromkeys([image_path, *renames]))


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
    ``{"referrer", "target", "status": "updated" | "failed" | "skipped"}``,
    plus ``"reason"`` for failed and skipped ones. It is what the Ledger's
    ``referrers_updated`` column stores. ``failed``/``reason`` name the first
    failure, which is why the Source was kept.
    """

    records: list[dict] = field(default_factory=list)
    failed: str | None = None
    reason: str | None = None
    # The async applies a phase's thread left for the event loop, in order.
    pending: list[tuple[Referrer, ReferrerChange]] = field(
        default_factory=list, repr=False, compare=False,
    )

    @property
    def ok(self) -> bool:
        return self.failed is None

    @property
    def updated(self) -> list[dict]:
        return [r for r in self.records if r["status"] == "updated"]

    @property
    def kept_message(self) -> str:
        name = os.path.basename(self.failed or "")
        return f"converted, source kept: couldn't update {name} ({self.reason})"

    def _record(self, referrer: str, target: str, status: str, reason: str | None = None):
        record = {"referrer": referrer, "target": target, "status": status}
        if reason is not None:
            record["reason"] = reason
        self.records.append(record)

    def _fail(self, referrer: str, exc: Exception, fallback_target: str) -> None:
        if isinstance(exc, ReferrerError):
            target, reason = exc.target, exc.reason
        else:
            target, reason = fallback_target, str(exc) or type(exc).__name__
        self._record(referrer, target, "failed", reason)
        self.failed, self.reason = target, reason


_SKIPPED = "not attempted after an earlier Referrer failed"


def _update_sync(
    image_path: str, renames: Mapping[str, str],
    abandoned: threading.Event | None = None,
    referrers: list[Referrer] | None = None,
) -> ReferrerOutcome:
    """Plan *referrers* (one phase; by default every always-on Referrer), then
    apply each change. The first failure stops it.

    Every write is guarded (``base.check_write``, under the target's lock):
    once *abandoned* is set, or an output no longer has the identity it had
    when this started, nothing more is written. An ``async def`` apply is not
    called here: it is left in ``outcome.pending`` for the event loop.
    """
    guard = WriteGuard(
        abandoned=abandoned or threading.Event(),
        outputs={new: file_identity(new) for new in dict.fromkeys(renames.values())},
    )
    if referrers is None:
        referrers = registry.active(None)
    with write_guard(guard):
        return _plan_and_apply(image_path, renames, referrers)


def _is_async(referrer: Referrer) -> bool:
    return inspect.iscoroutinefunction(referrer.apply)


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
            referrer.apply(change)
        except Exception as exc:
            # Any failure keeps the Source, so the catch is deliberately broad.
            logger.warning(
                "Referrer %s could not update %s", referrer.id, change.target, exc_info=True,
            )
            outcome._fail(referrer.id, exc, change.target)
            continue
        outcome._record(referrer.id, change.target, "updated")
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


async def _await_bounded(
    referrer: Referrer, change: ReferrerChange, *,
    cancel_event: asyncio.Event | None, bound: float | None,
) -> None:
    """Await an async apply on the loop, bounded and raced against Cancel.

    Past the bound or on a Cancel the apply is cancelled (not abandoned: it
    stops at its next ``await``) and ReferrerError names the target.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ReferrerError(change.target, "cancelled")
    limit = _limit(bound)
    task = asyncio.ensure_future(referrer.apply(change))
    waiters = {task}
    if cancel_event is not None:
        waiters.add(asyncio.ensure_future(cancel_event.wait()))
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=limit, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for waiter in waiters:
            waiter.cancel()
    if task in done:
        task.result()
        return
    if cancel_event is not None and cancel_event.is_set():
        raise ReferrerError(change.target, "cancelled")
    raise ReferrerError(change.target, f"did not answer within {limit:g}s")


async def _apply_pending(
    outcome: ReferrerOutcome, pending: list[tuple[Referrer, ReferrerChange]], *,
    cancel_event: asyncio.Event | None, bound: float | None,
) -> None:
    """Apply a phase's async changes in order; the first failure stops it."""
    for referrer, change in pending:
        if not outcome.ok:
            outcome._record(referrer.id, change.target, "skipped", _SKIPPED)
            continue
        try:
            await _await_bounded(referrer, change, cancel_event=cancel_event, bound=bound)
        except Exception as exc:
            # Any failure keeps the Source, so the catch is deliberately broad.
            logger.warning(
                "Referrer %s could not update %s", referrer.id, change.target, exc_info=True,
            )
            outcome._fail(referrer.id, exc, change.target)
            continue
        outcome._record(referrer.id, change.target, "updated")


async def _run_phase(
    image_path: str, renames: dict[str, str], abandoned: threading.Event,
    referrers: list[Referrer], *, cancel_event, bound: float | None,
) -> ReferrerOutcome:
    """One phase's plans and blocking applies, on a detached thread, bounded."""
    if bound is not None:
        # A caller with its own, tighter bound (a retried delete, #329).
        return await asyncio.wait_for(
            run_detached(
                _update_sync, image_path, renames, abandoned, referrers,
                cancel_event=cancel_event,
            ),
            bound,
        )
    return await run_detached_within_stall_bound(
        _update_sync, image_path, renames, abandoned, referrers,
        what=f"Updating {referrers[0].phase.target}", cancel_event=cancel_event,
    )


async def update_before_source_removal(
    image_path: str, renames: Mapping[str, str], *, library=None,
    cancel_event=None, bound: float | None = None,
) -> ReferrerOutcome:
    """Run every Referrer that is on for an Image whose Source is about to be removed.

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
    """
    outcome = ReferrerOutcome()
    try:
        phases = _by_phase(registry.active(library))
    except ReferrerError as exc:
        logger.warning("Referrer hook for %s: %s", image_path, exc)
        outcome._fail("library", exc, image_path)
        return outcome
    abandoned = threading.Event()
    renames = dict(renames)
    for referrers in phases:
        if not outcome.ok:
            # A later phase never runs after an earlier one failed.
            for referrer in referrers:
                outcome._record(referrer.id, image_path, "skipped", _SKIPPED)
            continue
        try:
            stage = await _run_phase(
                image_path, renames, abandoned, referrers,
                cancel_event=cancel_event, bound=bound,
            )
        except ReadCancelled:
            reason = "cancelled"
            logger.warning("Referrer hook for %s did not finish: cancelled", image_path)
        except Exception as exc:
            # A timeout or anything else keeps the Source, so the catch is broad.
            reason = str(exc) or type(exc).__name__
            logger.warning(
                "Referrer hook for %s did not finish", image_path, exc_info=True,
            )
        else:
            outcome.records.extend(stage.records)
            outcome.failed, outcome.reason = stage.failed, stage.reason
            await _apply_pending(
                outcome, stage.pending, cancel_event=cancel_event, bound=bound,
            )
            continue
        abandoned.set()
        # One failed record per Referrer of the phase, so the Ledger row says
        # why the Source was kept even though no target was reached.
        for referrer in referrers:
            outcome._record(referrer.id, image_path, "failed", reason)
        outcome.failed, outcome.reason = referrers[0].phase.target, reason
    return outcome


async def plan_changes_bounded(
    image_path: str, renames: Mapping[str, str], library=None,
) -> list[ReferrerChange]:
    """``plan_changes`` for the delete confirmation, bounded like the job's
    hook: on a detached thread under the conversion's no-progress bound, so a
    playlist on a mount that stopped answering can't hang the confirmation.
    *library* is the Image's Library, as the job's hook gets it. Raises
    ReferrerError (target "playlists") on a timeout."""
    try:
        return await run_detached_within_stall_bound(
            plan_changes, image_path, dict(renames), library, what="Reading playlists",
        )
    except ReferrerError:
        raise
    except Exception as exc:
        raise ReferrerError("playlists", str(exc) or type(exc).__name__) from exc


__all__ = [
    "Phase",
    "Referrer",
    "ReferrerChange",
    "ReferrerError",
    "ReferrerOutcome",
    "ReferrerRegistry",
    "atomic_write_bytes",
    "plan_changes",
    "plan_changes_bounded",
    "registry",
    "update_before_source_removal",
    "write_new_bytes",
]
