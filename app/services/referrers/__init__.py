"""Referrer registry and the one hook the job pipeline calls (#295).

Referrers are registered here, like tools in ``services.tools``. Callers never
branch on a Referrer's identity: they ask for every Referrer's planned change
(``plan_changes``, used by the delete confirmation) or run them all
(``update_before_source_removal``, used by the job before it deletes a Source).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger

from .base import Referrer, ReferrerChange, ReferrerError, atomic_write_bytes, write_new_bytes
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


def _update_sync(image_path: str, renames: Mapping[str, str]) -> ReferrerOutcome:
    """Plan every Referrer, then apply each change. The first failure stops it."""
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
    image_path: str, renames: Mapping[str, str],
) -> ReferrerOutcome:
    """Run every Referrer for an Image whose Source is about to be removed.

    *renames* maps each Source file that will be deleted to the output that
    replaces it. Stops at the first failure: the caller must then keep the
    Source. Never raises.
    """
    return await run_in_threadpool(_update_sync, image_path, dict(renames))


__all__ = [
    "Referrer",
    "ReferrerChange",
    "ReferrerError",
    "ReferrerOutcome",
    "atomic_write_bytes",
    "plan_changes",
    "registry",
    "update_before_source_removal",
    "write_new_bytes",
]
