"""Referrer registry and the one hook the job pipeline calls (#295).

Referrers are registered here, like tools in ``services.tools``. Callers never
branch on a Referrer's identity: they ask for every Referrer's planned change
(``plan_changes``, used by the delete confirmation) or run them all
(``update_before_source_removal``, used by the job before it deletes a Source).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from logging_setup import get_logger
from services.subprocess_runner import ReadCancelled, run_detached_within_stall_bound

from .base import (
    Referrer,
    ReferrerChange,
    ReferrerError,
    WriteGuard,
    atomic_write_bytes,
    file_identity,
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
        self._referrers[referrer.id] = referrer

    def all(self) -> list[Referrer]:
        return list(self._referrers.values())


registry = ReferrerRegistry()
registry.register(M3uReferrer())


def _applies(referrer: Referrer, image_path: str, renames: Mapping[str, str]) -> bool:
    """Whether *referrer* could point at the Image or any file being replaced.

    Every deleted path is asked, not just the Image's own: a cue's track in a
    subfolder can have a playlist of its own.
    """
    return any(referrer.detect(p) for p in dict.fromkeys([image_path, *renames]))


def plan_changes(image_path: str, renames: Mapping[str, str]) -> list[ReferrerChange]:
    """Every registered Referrer's planned change for *renames*. Writes nothing.

    Raises ReferrerError when a Referrer can't tell what it would change.
    """
    changes: list[ReferrerChange] = []
    for referrer in registry.all():
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
        logger.warning("Referrer %s could not update %s: %s", referrer, target, reason)
        self._record(referrer, target, "failed", reason)
        self.failed, self.reason = target, reason


_SKIPPED = "not attempted after an earlier Referrer failed"


def _update_sync(
    image_path: str, renames: Mapping[str, str],
    abandoned: threading.Event | None = None,
) -> ReferrerOutcome:
    """Plan every Referrer, then apply each change. The first failure stops it.

    Every write is guarded (``base.check_write``, under the target's lock):
    once *abandoned* is set, or an output no longer has the identity it had
    when this started, nothing more is written.
    """
    guard = WriteGuard(
        abandoned=abandoned or threading.Event(),
        outputs={new: file_identity(new) for new in dict.fromkeys(renames.values())},
    )
    with write_guard(guard):
        return _plan_and_apply(image_path, renames)


def _plan_and_apply(image_path: str, renames: Mapping[str, str]) -> ReferrerOutcome:
    outcome = ReferrerOutcome()
    planned: list[tuple[Referrer, ReferrerChange]] = []
    for referrer in registry.all():
        try:
            if _applies(referrer, image_path, renames):
                planned.extend((referrer, c) for c in referrer.plan(renames))
        except Exception as exc:  # noqa: BLE001 - any failure keeps the Source
            outcome._fail(referrer.id, exc, referrer.id)
            break
    for referrer, change in planned:
        if not outcome.ok:
            outcome._record(referrer.id, change.target, "skipped", _SKIPPED)
            continue
        try:
            referrer.apply(change)
        except Exception as exc:  # noqa: BLE001 - any failure keeps the Source
            outcome._fail(referrer.id, exc, change.target)
            continue
        outcome._record(referrer.id, change.target, "updated")
    return outcome


async def update_before_source_removal(
    image_path: str, renames: Mapping[str, str], *, cancel_event=None,
) -> ReferrerOutcome:
    """Run every Referrer for an Image whose Source is about to be removed.

    *renames* maps each Source file that will be deleted to the output that
    replaces it. Stops at the first failure: the caller must then keep the
    Source. Never raises.

    Bounded and cancellable like the job's other reads of a mount that may
    stop answering: the work runs on a detached thread under the conversion's
    no-progress bound, and *cancel_event* returns at once. On a timeout or a
    cancel the thread is abandoned and the outcome keeps the Source. The
    abandoned thread is told so: it checks under each playlist's lock before
    every write and writes nothing more, so it can't point a playlist at an
    output another job has since replaced.
    """
    abandoned = threading.Event()
    try:
        return await run_detached_within_stall_bound(
            _update_sync, image_path, dict(renames), abandoned,
            what="Updating playlists", cancel_event=cancel_event,
        )
    except ReadCancelled:
        reason = "cancelled"
    except Exception as exc:  # noqa: BLE001 - a timeout or anything else keeps the Source
        reason = str(exc) or type(exc).__name__
    abandoned.set()
    logger.warning("Referrer hook for %s did not finish: %s", image_path, reason)
    outcome = ReferrerOutcome()
    # One failed record per Referrer, so the Ledger row says why the Source
    # was kept even though no target was reached.
    for referrer in registry.all():
        outcome._record(referrer.id, image_path, "failed", reason)
    outcome.failed, outcome.reason = "playlists", reason
    return outcome


async def plan_changes_bounded(
    image_path: str, renames: Mapping[str, str],
) -> list[ReferrerChange]:
    """``plan_changes`` for the delete confirmation, bounded like the job's
    hook: on a detached thread under the conversion's no-progress bound, so a
    playlist on a mount that stopped answering can't hang the confirmation.
    Raises ReferrerError (target "playlists") on a timeout."""
    try:
        return await run_detached_within_stall_bound(
            plan_changes, image_path, dict(renames), what="Reading playlists",
        )
    except ReferrerError:
        raise
    except Exception as exc:
        raise ReferrerError("playlists", str(exc) or type(exc).__name__) from exc


__all__ = [
    "Referrer",
    "ReferrerChange",
    "ReferrerError",
    "ReferrerOutcome",
    "atomic_write_bytes",
    "plan_changes",
    "plan_changes_bounded",
    "registry",
    "update_before_source_removal",
    "write_new_bytes",
]
