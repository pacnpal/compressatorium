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
* inconclusive (the check could not run, timed out, its verifier was killed by
  a signal, or it was cancelled; #306's verdict rules decide): nothing is
  written, so it stays as stale as it was and gets no Finding.

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
from services.image_sets import image_fingerprint, image_members
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.rules.library import Image, Library
from services.subprocess_runner import (
    collect_abandonment,
    run_detached,
    stops_verify_batch,
    verify_exception_event,
)
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
    # Finding's cause, so a dismissal holds while the file stays the same, and
    # what the run compares with just before it checks.
    fingerprint: str
    # The Image's other files (a sheet's tracks, split parts): locked with it.
    members: tuple[str, ...] = ()


# (library id, image key) of every Image picked and not yet checked, so a Run
# now during a running check does not pick the same Image twice.
_in_flight: set[tuple[str, str]] = set()


def in_flight(library_id: str, image_key: str) -> bool:
    return (library_id, str(image_key)) in _in_flight


async def order_stamps(library_id: str) -> dict[str, str]:
    """``{image key: the later of its last check and last attempt}`` for *library_id*."""
    return await run_in_threadpool(image_checks.order_stamps, library_id)


def stalest_first(images: list[Image], last: dict[str, str]) -> list[Image]:
    """Never-checked Images first, then the oldest of last check or attempt, then by key.

    *last* is :func:`order_stamps`. An Image whose check was only ever
    inconclusive has an attempt time, so it goes behind the Images never
    tried until they have all had a turn.
    """

    def order(image: Image) -> tuple:
        stamp = last.get(str(image.key))
        return (stamp is not None, stamp or "", str(image.key))

    return sorted(images, key=order)


def inspect(image: Image) -> dict[str, Any]:
    """Every disk question about one candidate, in one hop. Blocking.

    ``{"skip": reason}`` when it cannot be checked here (``unresolvable``:
    no local path inside the Volumes; ``missing``: gone from disk;
    ``uncheckable``: no tool checks this file on its Platform, a folder
    Image included), else ``{"resolved": path, "check": Check}``.
    """
    path = image.path
    if not path or not is_within_configured_volumes(path):
        return {"skip": "unresolvable"}
    try:
        resolved = str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return {"skip": "unresolvable"}
    if os.path.isdir(resolved):
        return {"skip": "uncheckable"}  # no tool verifies a folder
    if not os.path.isfile(resolved):
        return {"skip": "missing"}
    tool = registry.check_tool(path, image.platform)
    if tool is None:
        return {"skip": "uncheckable"}
    return {
        "resolved": resolved,
        "check": Check(
            key=str(image.key), name=os.path.basename(path), path=path,
            # A Library that does not know the size (RomM without
            # fs_size_bytes) still pays for the Image against the budget.
            size=int(image.size or os.path.getsize(resolved)), tool_id=tool.id,
            fingerprint=image_fingerprint(path) or "",
            members=tuple(image_members(path)),
        ),
    }


def _hold(check: Check) -> list[str] | None:
    """Take #306's shared input lock on the Image and its files, or none. Blocking.

    Held for the whole check, so a delete-source job (in this process or
    another) sees the Image in use and keeps it. None when one cannot be had:
    a job is writing that path.
    """
    held: list[str] = []
    for path in dict.fromkeys((check.path, *check.members)):
        if not lock_manager.acquire_input_lock(path):
            _let_go(held)
            return None
        held.append(path)
    return held


def _let_go(held: list[str]) -> None:
    for path in held:
        lock_manager.release_input_lock(path)


def _unchanged(check: Check) -> bool:
    """Whether the Image is still the one the sweep picked. Blocking.

    A conversion Rule's delete-source, or the user, can remove or replace it
    while the run works through the Images before it.
    """
    return os.path.isfile(check.path) and image_fingerprint(check.path) == check.fingerprint


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
# Gone, replaced or being written when its turn came: not checked at all.
SKIPPED = "skipped"
# How long the re-check before each Image may take (a stat and a fingerprint).
_RECHECK_SECONDS = 30

notifications.register_event(
    "check_failed", "A check Rule found Images that are not Intact (one message per sweep)",
)

_tasks: set[asyncio.Task] = set()


def outcome(result: dict) -> str:
    """What a Verify result says about the Image.

    ``cancelled`` proved nothing (the tool contract, issue #266). A result
    #306 flags ``inconclusive`` (the check could not run: a verifier killed by
    a signal through ``verify_exit_event``, an allowlisted exception through
    ``verify_exception_event``, a timeout or stall) or ``abandoned`` (the
    verifier outlived SIGKILL) is no verdict either. Otherwise ``valid`` decides.
    """
    if result.get("cancelled"):
        return CANCELLED
    if result.get("inconclusive") or result.get("abandoned"):
        return INCONCLUSIVE
    return INTACT if result.get("valid") else NOT_INTACT


async def _verify(
    check: Check, cancel_event: asyncio.Event | None,
) -> tuple[str, str, bool]:
    """Run the tool's Verify on one Image: ``(outcome, message, stop)``.

    The same call, lane and bound as the Verify action. An exception the check
    raises (its bound expiring included) becomes a result through #306's
    ``verify_exception_event``, whose allowlist decides whether it is a verdict
    on the file. ``stop`` is #306's ``stops_verify_batch``: something is still
    reading the storage, so the run must not open the next file.
    """
    tool = registry.get(check.tool_id)
    token = await workload_limiter.acquire("verify")
    async with token:
        with collect_abandonment() as abandoned:
            try:
                bound = await tool.verify_timeout(check.path, cancel_event=cancel_event)
                result = await asyncio.wait_for(
                    tool.verify(check.path, cancel_event=cancel_event), timeout=bound or None,
                )
            except Exception as exc:  # noqa: BLE001 - #306's allowlist sorts it
                result = verify_exception_event(exc, prefix="The check raised")
    result = result if isinstance(result, dict) else {}
    return (
        outcome(result), str(result.get("message") or ""),
        stops_verify_batch(result, abandoned),
    )


def start(library: Library, picked: list[Check], *, generation: int) -> Any:
    """Check *picked* in the background, as one external job. Returns the job.

    *generation* is the Library's (``sweep.library_generation``) when the
    sweep picked them: once it moves (the Library was removed) nothing more
    is written for it.
    """
    count = len(picked)
    job = job_manager.create_external_job(
        f"Check {count} Image{'s' if count != 1 else ''} in {library.name}",
        ConversionMode.CHECK,
        message="Waiting to check",
    )
    for check in picked:
        _in_flight.add((library.id, check.key))
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
    """Write one verdict: the last check or attempt time and the Finding. Blocking."""
    if verdict == INCONCLUSIVE:
        # Only orders the next picks; no check time, no Finding, nothing cleared.
        image_checks.record_attempt(library_id, check.key, current=current)
    elif verdict == INTACT:
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
    counts = {INTACT: 0, NOT_INTACT: 0, INCONCLUSIVE: 0, SKIPPED: 0}
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
                verdict, message, stop = await _check_one(check, cancel_event)
            finally:
                _in_flight.discard((library.id, check.key))
            if verdict == SKIPPED:
                counts[SKIPPED] += 1
                continue
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
            if stop:
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
        + (f", {counts[SKIPPED]} gone or changed" if counts[SKIPPED] else "")
    )
    if cancelled:
        await job_manager.finish_external_job_cancelled(job_id, message=f"Stopped: {summary}")
        return
    job = job_manager.get_job(job_id)
    if job is not None:
        job.message = summary
    await job_manager.finish_external_job(job_id, success=True)


async def _check_one(
    check: Check, cancel_event: asyncio.Event | None,
) -> tuple[str, str, bool]:
    """Check one picked Image, if it is still the one picked: ``(outcome, message, stop)``.

    The Image and its files are locked for the whole check (:func:`_hold`), then
    compared with what the sweep picked (:func:`_unchanged`). Gone, changed or
    being written is ``SKIPPED``: no check time, no Finding, no notification.
    """
    held = await run_in_threadpool(_hold, check)
    if held is None:
        return SKIPPED, "", False
    try:
        try:
            same = await asyncio.wait_for(
                run_detached(_unchanged, check), _RECHECK_SECONDS,
            )
        except (asyncio.TimeoutError, OSError):
            same = False
        if not same:
            return SKIPPED, "", False
        return await _verify(check, cancel_event)
    finally:
        await run_in_threadpool(_let_go, held)


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
