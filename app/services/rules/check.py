"""Check Rules (#305): check the stalest Images of a Platform within a nightly budget.

A check Rule is a Rule with no target format (``rules.is_check_rule``). The
sweep (:mod:`services.rules.sweep`) lists its Platform's Images through the
Library interface, orders them stalest first (never checked, then the oldest
last check, from :mod:`services.image_checks`) and picks them until the Rule's
byte budget is used. An Image no tool can check is skipped and costs nothing.

This module holds what a check needs once picked: :func:`inspect` (one
Image's disk questions, off the loop) and :func:`start` (the run). A run is
one external job in the job queue, so it shows in the Jobs panel and can be
cancelled. Each Image goes through its tool's own Verify, the same call the
Verify action makes, on the same ``verify`` workload lane, bounded by the same
``verify_timeout``:

* passed: the Image is Intact. Its last check time moves, its Verify record
  is written and a "not Intact" Finding it had is cleared;
* failed: its last check time moves and it gets a "not Intact" Finding;
* inconclusive (the check could not run, timed out or was cancelled): nothing
  is written, so it stays as stale as it was and gets no Finding.

A run with failures sends one ``check_failed`` notification listing them.
Nothing here moves, renames or deletes an Image.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import findings, image_checks
from services.image_sets import image_fingerprint
from services.rules.library import Image
from services.tools import registry
from utils.path_utils import is_within_configured_volumes

logger = get_logger("romm_auto")

GB = 1024 ** 3


@dataclass(frozen=True)
class Check:
    """One Image a check Rule picked."""

    key: str
    name: str
    path: str
    size: int
    tool_id: str
    # The Image's files (size and mtime) when it was picked: a "not Intact"
    # Finding's cause, so a dismissal holds while the file stays the same.
    fingerprint: str


# (library id, image key) of every Image picked and not yet checked, so a Run
# now during a running check does not pick the same Image twice.
_in_flight: set[tuple[str, str]] = set()


def in_flight(library_id: str, image_key: str) -> bool:
    return (library_id, str(image_key)) in _in_flight


async def last_checked(library_id: str) -> dict[str, str]:
    """``{image key: last check time}`` for *library_id*."""
    return await run_in_threadpool(image_checks.last_checked, library_id)


def stalest_first(images: list[Image], last: dict[str, str]) -> list[Image]:
    """Never-checked Images first, then the oldest last check, then by key."""

    def order(image: Image) -> tuple:
        stamp = last.get(str(image.key))
        return (stamp is not None, stamp or "", str(image.key))

    return sorted(images, key=order)


def inspect(image: Image) -> dict[str, Any]:
    """Every disk question about one candidate, in one hop. Blocking.

    ``{"skip": reason}`` when it cannot be checked here (``unresolvable``:
    no local path inside the Volumes; ``missing``: gone from disk;
    ``uncheckable``: no tool checks this file on its Platform), else
    ``{"resolved": path, "check": Check}``.
    """
    path = image.path
    if not path or not is_within_configured_volumes(path):
        return {"skip": "unresolvable"}
    try:
        resolved = str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return {"skip": "unresolvable"}
    if not os.path.isfile(resolved):
        return {"skip": "missing"}
    tool = registry.check_tool(path, image.platform)
    if tool is None:
        return {"skip": "uncheckable"}
    return {
        "resolved": resolved,
        "check": Check(
            key=str(image.key), name=os.path.basename(path), path=path,
            size=int(image.size or 0), tool_id=tool.id,
            fingerprint=image_fingerprint(path) or "",
        ),
    }


def finding_for(check: Check, reason: str) -> findings.Observed:
    """The "not Intact" Finding for a failed check."""
    return findings.Observed(
        kind=findings.NOT_INTACT, image_key=check.key, path=check.path,
        reason=reason, cause={"tool": check.tool_id, "files": check.fingerprint},
    )
