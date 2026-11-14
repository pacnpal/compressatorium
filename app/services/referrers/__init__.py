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

from .base import Referrer, ReferrerChange, ReferrerError, atomic_write_bytes
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


def plan_changes(image_path: str, renames: Mapping[str, str]) -> list[ReferrerChange]:
    """Every registered Referrer's planned change for *renames*. Writes nothing.

    Raises ReferrerError when a Referrer can't tell what it would change.
    """
    changes: list[ReferrerChange] = []
    for referrer in registry.all():
        if referrer.detect(image_path):
            changes.extend(referrer.plan(renames))
    return changes


@dataclass
class ReferrerOutcome:
    """What the hook did. ``failed`` names the target that couldn't be updated."""

    updated: list[dict] = field(default_factory=list)
    failed: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed is None

    @property
    def kept_message(self) -> str:
        return f"converted, source kept: couldn't update {os.path.basename(self.failed or '')}"


def _update_sync(image_path: str, renames: Mapping[str, str]) -> ReferrerOutcome:
    outcome = ReferrerOutcome()
    for referrer in registry.all():
        target = referrer.id
        try:
            if not referrer.detect(image_path):
                continue
            for change in referrer.plan(renames):
                target = change.target
                referrer.apply(change)
                outcome.updated.append({"referrer": referrer.id, "target": change.target})
        except Exception as exc:  # noqa: BLE001 - any failure keeps the Source
            failed = exc.target if isinstance(exc, ReferrerError) else target
            logger.warning("Referrer %s could not update %s: %s", referrer.id, failed, exc)
            outcome.failed = failed
            return outcome
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
]
