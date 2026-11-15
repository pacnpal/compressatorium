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
from models import ConversionMode
from services import findings, image_checks, notifications
from services.image_sets import image_fingerprint
from services.job_manager import job_manager
from services.rules.library import Image, Library
from services.tools import registry
from services.verification_store import verification_store
from services.workload_limiter import workload_limiter
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


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------

INTACT = image_checks.INTACT
NOT_INTACT = image_checks.NOT_INTACT
INCONCLUSIVE = "inconclusive"
CANCELLED = "cancelled"

notifications.register_event(
    "check_failed", "A check Rule found Images that are not Intact (one message per sweep)",
)

_tasks: set[asyncio.Task] = set()


def outcome(result: dict) -> str:
    """What a Verify result says about the Image.

    ``cancelled`` proved nothing (the tool contract, issue #266). A result
    flagged ``inconclusive`` (the check could not run) or ``abandoned`` (the
    verifier outlived SIGKILL, so the storage is stuck) is no verdict either.
    Otherwise ``valid`` decides.
    """
    if result.get("cancelled"):
        return CANCELLED
    if result.get("inconclusive") or result.get("abandoned"):
        return INCONCLUSIVE
    return INTACT if result.get("valid") else NOT_INTACT


async def _verify(check: Check, cancel_event: asyncio.Event | None) -> tuple[str, str, dict]:
    """Run the tool's Verify on one Image: ``(outcome, message, result)``.

    The same call, lane and bound as the Verify action. A timeout or an
    exception escaping the tool is a check that could not finish: inconclusive.
    """
    tool = registry.get(check.tool_id)
    token = await workload_limiter.acquire("verify")
    async with token:
        try:
            bound = await tool.verify_timeout(check.path, cancel_event=cancel_event)
            result = await asyncio.wait_for(
                tool.verify(check.path, cancel_event=cancel_event), timeout=bound or None,
            )
        except asyncio.TimeoutError:
            return INCONCLUSIVE, "The check did not finish in time", {}
        except Exception as exc:  # noqa: BLE001  no verdict from a check that broke
            logger.warning(
                "romm_auto: checking %s raised %s", check.path, type(exc).__name__,
                exc_info=True,
            )
            return INCONCLUSIVE, f"The check could not run ({type(exc).__name__})", {}
    result = result if isinstance(result, dict) else {}
    return outcome(result), str(result.get("message") or ""), result


def start(library: Library, picked: list[Check], *, generation: int) -> Any:
    """Check *picked* in the background, as one external job. Returns the job.

    *generation* is the Library's (``sweep.library_generation``) when the
    sweep picked them: once it moves (the Library was removed) nothing more
    is written for it.
    """
    for check in picked:
        _in_flight.add((library.id, check.key))
    count = len(picked)
    job = job_manager.create_external_job(
        f"Check {count} Image{'s' if count != 1 else ''} in {library.name}",
        ConversionMode.CHECK,
        message="Waiting to check",
    )
    task = asyncio.get_running_loop().create_task(
        _run(library, picked, job.id, generation),
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def drain() -> None:
    """Wait for every running check (tests and shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def _apply(library_id: str, check: Check, verdict: str, message: str, current) -> None:
    """Write one verdict: the last check time and the Finding. Blocking."""
    if verdict == INTACT:
        image_checks.record(library_id, check.key, INTACT, current=current)
        findings.resolve(library_id, check.key, findings.NOT_INTACT, current=current)
    elif verdict == NOT_INTACT:
        image_checks.record(library_id, check.key, NOT_INTACT, current=current)
        findings.observe(
            library_id, finding_for(check, message or "The check failed"), current=current,
        )


async def _run(library: Library, picked: list[Check], job_id: str, generation: int) -> None:
    from services.rules.sweep import library_generation

    def current() -> bool:
        return library_generation(library.id) == generation

    cancel_event = job_manager.get_cancel_event(job_id)
    counts = {INTACT: 0, NOT_INTACT: 0, INCONCLUSIVE: 0}
    failed: list[tuple[Check, str]] = []
    cancelled = False
    try:
        for index, check in enumerate(picked):
            if job_manager.is_cancelled(job_id) or not current():
                cancelled = True
                break
            await job_manager.update_external_job(
                job_id, progress=int(100 * index / max(1, len(picked))),
                message=f"Checking {index + 1} of {len(picked)}: {check.name}",
            )
            try:
                verdict, message, result = await _verify(check, cancel_event)
            finally:
                _in_flight.discard((library.id, check.key))
            if verdict == CANCELLED:
                cancelled = True
                break
            counts[verdict] += 1
            if verdict == INTACT:
                # What the Verify action records for a pass.
                await verification_store.mark_verified(check.path)
            elif verdict == NOT_INTACT:
                # The Intact badge no longer holds.
                await verification_store.clear(check.path)
                failed.append((check, message))
            await run_in_threadpool(_apply, library.id, check, verdict, message, current)
            if result.get("abandoned"):
                # Something is still reading this storage; opening the next
                # file would strand one more reader.
                break
    except Exception:
        logger.warning("romm_auto: the check run for %s failed", library.id, exc_info=True)
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The check run stopped on an error",
        )
        return
    finally:
        for check in picked:
            _in_flight.discard((library.id, check.key))
        if failed and current():
            notifications.notify(check_failed_event(library, failed))
    summary = (
        f"{counts[INTACT]} Intact, {counts[NOT_INTACT]} not Intact"
        + (f", {counts[INCONCLUSIVE]} could not be checked" if counts[INCONCLUSIVE] else "")
    )
    if cancelled:
        await job_manager.finish_external_job_cancelled(job_id, message=f"Stopped: {summary}")
        return
    job = job_manager.get_job(job_id)
    if job is not None:
        job.message = summary
    await job_manager.finish_external_job(job_id, success=True)


def check_failed_event(library: Library, failed: list[tuple[Check, str]]) -> notifications.Event:
    """The one message a sweep's check run sends when Images failed."""
    count = len(failed)
    noun = "Image is" if count == 1 else "Images are"
    lines = [f"{count} {noun} not Intact in {library.name}:"]
    shown = failed[: notifications.MAX_LISTED_FAILURES]
    lines += [f"- {check.name}: {message or 'the check failed'}" for check, message in shown]
    if count > len(shown):
        lines.append(f"- and {count - len(shown)} more")
    return notifications.Event(
        "check_failed",
        f"Check failed: {count} {noun} not Intact",
        "\n".join(lines),
        {
            "library_id": library.id,
            "failed": [
                {"image": check.key, "path": check.path, "error": message}
                for check, message in failed
            ],
        },
    )
