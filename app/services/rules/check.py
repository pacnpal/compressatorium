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
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionMode
from services import findings, image_checks, notifications
from services.image_sets import image_fingerprint, image_members, image_size
from services.job_manager import CANCEL_SHUTDOWN, job_manager
from services.lock_manager import lock_manager
from services.rules.library import Image, Library
from services.subprocess_runner import (
    collect_abandonment,
    run_detached,
    stops_verify_batch,
    verify_exception_event,
)
from services.tools import registry
from services.verification_store import cannot_check, verification_store
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
    # Where its Verify record lives when that is not ``path``: a split output's
    # check is recorded under the logical ``Game.iso``, not ``Game.iso.0``.
    record_path: str = ""

    @property
    def record(self) -> str:
        return self.record_path or self.path


# (library id, image key) of every Image picked and not yet checked -> its
# files (the Image and its members), so a Run now during a running check does
# not pick the same Image twice, and a conversion Rule does not queue a Source
# a running check holds (#330, through `in_flight_paths`).
_in_flight: dict[tuple[str, str], tuple[str, ...]] = {}


def in_flight(library_id: str, image_key: str) -> bool:
    return (library_id, str(image_key)) in _in_flight


def in_flight_paths() -> list[str]:
    """Every file of every Image a running check picked and has not finished,
    as picked (unresolved). The sweep resolves them with the queued and
    converting Sources, so a conversion skips them the same way (#330)."""
    return [path for files in list(_in_flight.values()) for path in files]


async def order_stamps(library_id: str) -> dict[str, str]:
    """``{image key: the later of its last check and last attempt}`` for *library_id*."""
    return await run_in_threadpool(image_checks.order_stamps, library_id)


def stalest_first(images: list[Image], last: dict[str, str]) -> list[Image]:
    """Never-checked Images first, then by the later of check and attempt times, oldest first.

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
    Image included; ``busy``: a job is writing one of its files, here or in
    another process), else ``{"resolved": path, "check": Check}``.
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
    # A split output (`Game.iso.0`, `.1`) is one Image whose record is kept
    # under its logical name; a stored check's mode picks the tool (#306),
    # even after a failed check (`mark_failed` keeps it).
    owner = registry.split_owner(path)
    record_path = owner[0] if owner else path
    made_by = verification_store.produced_meta_blocking(record_path) or {}
    tool = registry.check_tool(path, image.platform, recorded_mode=made_by.get("mode"))
    if tool is None:
        return {"skip": "uncheckable"}
    members = image_members(path)
    # Every file the check reads, not only the one the Library names: a
    # split part or track symlinked outside the Volumes skips the whole Image.
    if not all(is_within_configured_volumes(member) for member in members):
        return {"skip": "unresolvable"}
    # Being written (a rebuild holds the logical `Game.iso` alone): held for
    # now, which is not the same as nothing to check.
    if any(
        lock_manager.is_locked(each)
        for each in dict.fromkeys((record_path, path, *members))
    ):
        return {"skip": "busy"}
    return {
        "resolved": resolved,
        "check": Check(
            key=str(image.key), name=os.path.basename(path), path=path,
            # A Library that does not know the size (RomM without
            # fs_size_bytes) still pays for the whole Image against the
            # budget: every file, split parts and tracks included.
            size=int(image.size or image_size(path)), tool_id=tool.id,
            fingerprint=image_fingerprint(path) or "",
            members=tuple(members),
            record_path=record_path if owner else "",
        ),
    }


def _hold(check: Check) -> list[str] | None:
    """Take #306's shared input lock on the Image and its files, or none. Blocking.

    Taken once the check has its verify slot and held only while the Verify
    runs, so a delete-source job (in this process or another) sees the Image
    in use for exactly that long. None when one cannot be had: a job is
    writing that path. An acquisition that raises lets go of the ones already
    taken first.
    """
    held: list[str] = []
    ok = False
    # A split output also holds its logical `Game.iso` (`record_path`): a
    # directory-input rebuild locks only that path, so without it a rebuild
    # could rewrite the parts mid-check (Codex 4577570988). The set is #306's:
    # logical path, first part, then `registry.split_members(first)`, which
    # `members` already carries.
    paths = (check.record_path, check.path, *check.members) if check.record_path else (
        check.path, *check.members,
    )
    try:
        for path in dict.fromkeys(paths):
            if not lock_manager.acquire_input_lock(path):
                return None
            held.append(path)
        ok = True
        return held
    finally:
        if not ok:
            _let_go(held)


def _let_go(held: list[str]) -> None:
    for path in held:
        lock_manager.release_input_lock(path)


class _LockFailed:
    """Taking the Image's locks raised (the lock dir, a path the lock manager
    cannot encode): the check cannot run, which is no verdict on the Image."""

    def __init__(self, error: BaseException) -> None:
        self.message = f"The Image could not be locked for the check ({type(error).__name__})"


async def _take(check: Check) -> list[str] | None | _LockFailed:
    """:func:`_hold` off the loop, bounded and safe to cancel.

    On a detached thread, bounded by ``_RECHECK_SECONDS``: taking the locks
    resolves the path and walks its folders, and a mount that stops answering
    must cost this Image, not the run. None when the locks did not come in
    time (the Image is skipped), a :class:`_LockFailed` when taking them raised
    (whatever was taken is already released). If the caller gives up (the
    bound, or a task cancel) while the thread is still taking them, the thread
    lets go of them itself when it finishes; if it had already finished, they
    go here.
    """
    guard = threading.Lock()
    state: dict[str, Any] = {"abandoned": False, "held": None}

    def work() -> list[str] | None:
        held = _hold(check)
        with guard:
            if not state["abandoned"]:
                state["held"] = held
                return held
        if held:
            _let_go(held)
        return None

    try:
        return await asyncio.wait_for(run_detached(work), _RECHECK_SECONDS)
    except BaseException as exc:
        with guard:
            state["abandoned"] = True
            held, state["held"] = state["held"], None
        if held:
            _let_go(held)
        if isinstance(exc, asyncio.TimeoutError):
            return None
        if isinstance(exc, Exception):
            logger.warning(
                "romm_auto: locking %s for a check raised %s", check.path,
                type(exc).__name__, exc_info=True,
            )
            return _LockFailed(exc)
        raise


async def _release(held: list[str]) -> None:
    """Let go of *held*, even if the task is cancelled while it does."""
    await asyncio.shield(asyncio.ensure_future(run_in_threadpool(_let_go, held)))


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
# Each running check task's job id, so :func:`drain` can cancel them on a stop.
_task_jobs: dict[asyncio.Task, str] = {}
# How each check run ended, by job id (:func:`run_outcome`): its counts, and
# whether it was cancelled or crashed. Read by the CLI for its exit status.
_outcomes: dict[str, dict[str, Any]] = {}
# How often :func:`drain` looks for a stop.
_STOP_POLL = 0.2


def outcome(result: dict) -> str:
    """What a Verify result says about the Image.

    ``cancelled`` proved nothing (the tool contract, issue #266). A result
    #306 flags ``inconclusive`` (the check could not run: a verifier killed by
    a signal through ``verify_exit_event``, an allowlisted exception through
    ``verify_exception_event``, a timeout or stall) or ``abandoned`` (the
    verifier outlived SIGKILL) is no verdict either, and nor is ``uncheckable``
    (#306: a rule this app sets, such as the archive safety limits, refused the
    file; that says nothing about its data). Otherwise ``valid`` decides.
    """
    if result.get("cancelled"):
        return CANCELLED
    # The shared reading (#370): every producer marks a result with no
    # verdict itself, an abandoned verifier included (#374).
    if cannot_check(result):
        return INCONCLUSIVE
    return INTACT if result.get("valid") else NOT_INTACT


async def _verify(
    check: Check, cancel_event: asyncio.Event | None,
) -> tuple[str, str, bool]:
    """Run the tool's Verify on one Image: ``(outcome, message, stop)``.

    The same call and bound as the Verify action; the caller holds its lane. An exception the check
    raises (its bound expiring included) becomes a result through #306's
    ``verify_exception_event``, whose allowlist decides whether it is a verdict
    on the file. ``stop`` is #306's ``stops_verify_batch``: something is still
    reading the storage, so the run must not open the next file.
    """
    tool = registry.get(check.tool_id)
    with collect_abandonment() as abandoned:
        try:
            bound = await tool.verify_timeout(check.path, cancel_event=cancel_event)
            result = await asyncio.wait_for(
                tool.verify(check.path, cancel_event=cancel_event), timeout=bound or None,
            )
        except Exception as exc:
            # Any failure becomes a result: #306's allowlist decides whether it
            # is a verdict on the file. Logged with its traceback, since a
            # verifier that raises is rare and worth seeing.
            logger.warning(
                "romm_auto: checking %s raised %s", check.path, type(exc).__name__,
                exc_info=True,
            )
            result = verify_exception_event(exc, prefix="The check raised")
    result = result if isinstance(result, dict) else {}
    return (
        outcome(result), str(result.get("message") or ""),
        stops_verify_batch(result, abandoned),
    )


async def verify_file(
    path: str, tool_id: str, cancel_event: asyncio.Event | None = None,
) -> tuple[str, str, bool]:
    """One file's Verify, as a check Rule runs it: ``(outcome, message, stop)``.

    On the ``verify`` lane, through the same call and bound as :func:`_verify`.
    For a file nothing else holds yet: an Inbox's staged copy (#431), checked
    Intact before it is placed. ``CANCELLED`` when *cancel_event* fires first.
    ``stop`` is #306's ``stops_verify_batch``, kept for the caller: something
    is still reading the storage, so it must not open its next file.
    """
    token = await _acquire_slot(cancel_event)
    if token is None:
        return CANCELLED, "", False
    async with token:
        check = Check(
            key=path, name=os.path.basename(path), path=path, size=0,
            tool_id=tool_id, fingerprint="",
        )
        return await _verify(check, cancel_event)


def start(
    library: Library, picked: list[Check], *, generation: int,
    on_finished: Callable[[frozenset[str]], Awaitable[None]] | None = None,
) -> Any:
    """Check *picked* in the background, as one external job. Returns the job.

    *generation* is the Library's (``sweep.library_generation``) when the
    sweep picked them: once it moves (the Library was removed) nothing more
    is written for it. *on_finished* is awaited exactly once as the run ends,
    with the keys of the picked Images it attempted (a verdict either way, no
    verdict, or skipped as gone, changed or in use). A run cut short (a stop
    on an abandoned verifier, a cancel) attempted only some; one that crashed,
    or whose Library changed, reports none. The sweep records a check Rule's
    run only when every Image it picked was attempted, and lets the windows
    go either way, so a check that never reached an Image does not use up
    its night.
    """
    count = len(picked)
    job = job_manager.create_external_job(
        f"Check {count} Image{'s' if count != 1 else ''} in {library.name}",
        ConversionMode.CHECK,
        message="Waiting to check",
    )
    for check in picked:
        _in_flight[(library.id, check.key)] = (check.path, *check.members)
    task = asyncio.get_running_loop().create_task(
        _run(library, picked, job.id, generation, on_finished),
    )
    _tasks.add(task)
    _task_jobs[task] = job.id
    task.add_done_callback(_forget_task)
    return job


def _forget_task(task: asyncio.Task) -> None:
    _tasks.discard(task)
    _task_jobs.pop(task, None)


def run_outcome(job_id: str) -> dict[str, Any] | None:
    """How the check run *job_id* ended, once it has: ``{"counts": {...},
    "cancelled": bool, "crashed": bool}``, or None while it runs."""
    return _outcomes.get(job_id)


async def drain(should_stop: Callable[[], bool] | None = None) -> None:
    """Wait for every running check (the CLI's run-rules, tests, shutdown).

    With *should_stop*, a stop cancels the running checks' jobs, which end
    them promptly (their cancel event reaches the slot wait and the verifier)
    with nothing recorded for the Image in hand, and the wait goes on only
    until they have ended. Each Image's own check stays bounded by its tool's
    verify bound, so the wait is too.
    """
    cancelled = False
    while _tasks:
        pending = list(_tasks)
        if should_stop is None:
            await asyncio.gather(*pending, return_exceptions=True)
            continue
        await asyncio.wait(pending, timeout=_STOP_POLL)
        if not cancelled and should_stop():
            cancelled = True
            for job_id in list(_task_jobs.values()):
                await job_manager.cancel_job(job_id, reason=CANCEL_SHUTDOWN)


def _apply(library_id: str, check: Check, verdict: str, message: str, current) -> bool:
    """Write one verdict: the last check or attempt time and the Finding. Blocking.

    False when nothing was written: the Library changed (removed, or RomM
    pointed elsewhere) while the Image was being checked.
    """
    if verdict == INCONCLUSIVE:
        # Only orders the next picks; no check time, no Finding, nothing cleared.
        return image_checks.record_attempt(library_id, check.key, current=current)
    if not image_checks.record(library_id, check.key, verdict, current=current):
        return False
    if verdict == INTACT:
        findings.resolve(library_id, check.key, findings.NOT_INTACT, current=current)
        return True
    return findings.observe(
        library_id, finding_for(check, message or "The check failed"), current=current,
    ) is not None


async def _run(
    library: Library, picked: list[Check], job_id: str, generation: int,
    on_finished: Callable[[frozenset[str]], Awaitable[None]] | None = None,
) -> None:
    """One check run, telling *on_finished* what it attempted, exactly once."""
    reported = False

    async def report(attempted: frozenset[str]) -> None:
        nonlocal reported
        reported = True
        await _report_end(on_finished, attempted, library.id)

    try:
        await _run_checks(library, picked, job_id, generation, report)
    finally:
        if not reported:
            # Crashed, or the task itself was cancelled: no run to record, and
            # the Rules' windows go free.
            await _report_end(on_finished, frozenset(), library.id)


async def _report_end(
    on_finished: Callable[[frozenset[str]], Awaitable[None]] | None,
    attempted: frozenset[str], library_id: str,
) -> None:
    """Tell the sweep how a run ended. A failure to record must not leave the
    job unfinished: the verdicts are written, and the Rule is simply due again."""
    if on_finished is None:
        return
    try:
        await on_finished(attempted)
    except Exception:
        logger.warning(
            "romm_auto: recording the check run for %s failed", library_id, exc_info=True,
        )


async def _run_checks(
    library: Library, picked: list[Check], job_id: str, generation: int,
    report: Callable[[frozenset[str]], Awaitable[None]],
) -> None:
    from services.rules.sweep import library_generation

    def current() -> bool:
        return library_generation(library.id) == generation

    cancel_event = job_manager.get_cancel_event(job_id)
    counts = {INTACT: 0, NOT_INTACT: 0, INCONCLUSIVE: 0, SKIPPED: 0}
    failed: list[tuple[Check, str]] = []
    # The picked Images the run reached and settled, in any way.
    attempted: set[str] = set()
    # The Verify records whose state it changed, in order: on the terminal
    # event, so a page that missed a progress event re-reads them.
    settled: list[str] = []
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
            async def persist(verdict: str, message: str, check: Check = check) -> bool:
                """Write the verdict; called by `_check_one` with the locks still held."""
                if not await run_in_threadpool(
                    _apply, library.id, check, verdict, message, current,
                ):
                    return False
                if verdict == INTACT:
                    # A re-check refreshes the record and keeps what made the
                    # file (its Source and producing mode): only a conversion
                    # replaces it.
                    await verification_store.refresh_check(check.record)
                    await _publish(job_id, check, INTACT, settled)
                elif verdict == NOT_INTACT:
                    # The Intact badge no longer holds, but what made the file
                    # stays, so the next check still reaches its tool.
                    await verification_store.mark_failed(check.record)
                    await _publish(job_id, check, None, settled)
                return True

            try:
                verdict, message, stop, written = await _check_one(
                    check, cancel_event, persist,
                )
            finally:
                _in_flight.pop((library.id, check.key), None)
            if verdict == SKIPPED:
                attempted.add(check.key)
                counts[SKIPPED] += 1
                continue
            if verdict == CANCELLED:
                cancelled = True
                break
            if not written:
                # The Library changed under the run: nothing was written for
                # this Image, so it is not counted or reported, and the run ends.
                cancelled = True
                break
            attempted.add(check.key)
            counts[verdict] += 1
            if verdict == NOT_INTACT:
                failed.append((check, message))
            if stop:
                # Something is still reading this storage; opening the next
                # file would strand one more reader.
                break
    except Exception:
        logger.warning("romm_auto: the check run for %s failed", library.id, exc_info=True)
        _outcomes[job_id] = {"counts": dict(counts), "cancelled": False, "crashed": True}
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The check run stopped on an error",
            event_extra={"verification_paths": settled},
        )
        return
    finally:
        for check in picked:
            _in_flight.pop((library.id, check.key), None)
        if failed and current():
            notifications.notify(check_failed_event(library, failed))
    summary = (
        f"{counts[INTACT]} Intact, {counts[NOT_INTACT]} not Intact"
        + (f", {counts[INCONCLUSIVE]} could not be checked" if counts[INCONCLUSIVE] else "")
        + (f", {counts[SKIPPED]} gone, changed or in use" if counts[SKIPPED] else "")
    )
    _outcomes[job_id] = {"counts": dict(counts), "cancelled": cancelled, "crashed": False}
    if cancelled:
        # Only the Rules whose Images were all reached count as run.
        await report(frozenset(attempted) if current() else frozenset())
        await job_manager.finish_external_job_cancelled(
            job_id, message=f"Stopped: {summary}",
            event_extra={"verification_paths": settled},
        )
        return
    # The check Rules' runs, recorded for the Images their checks reached.
    await report(frozenset(attempted) if current() else frozenset())
    job = job_manager.get_job(job_id)
    if job is not None:
        job.message = summary
    await job_manager.finish_external_job(
        job_id, success=True, event_extra={"verification_paths": settled},
    )


async def _publish(
    job_id: str, check: Check, state: str | None, settled: list[str],
) -> None:
    """Tell an open page about one Image's new check state, as it settles.

    On the run's own ``progress`` event, which the jobs stream forwards in
    order: the Verify record's path, as ``/api/verified`` lists it, with its
    check now, or None once it is no longer Intact. Sent per Image rather than
    as a batch at the end, so a later pass on that file (a repair and a
    re-verify) is never overwritten by this run's older verdict. A stream
    that drops progress events (a full subscriber queue) is caught up by the
    terminal event's ``verification_paths``, which the page re-reads from the
    database (``/api/verified/lookup``).
    """
    path = os.path.realpath(check.record)
    if path not in settled:
        settled.append(path)
    await job_manager.update_external_job(
        job_id, event_extra={"verification_changes": {path: state}},
    )


async def _acquire_slot(cancel_event: asyncio.Event | None) -> Any:
    """A slot on the ``verify`` lane, or None when the job is cancelled first.

    A run can queue for a slot for as long as other verifies take; Cancel must
    end it now, not when a slot comes free.
    """
    if cancel_event is None:
        return await workload_limiter.acquire("verify")
    if cancel_event.is_set():
        return None
    slot = asyncio.ensure_future(workload_limiter.acquire("verify"))
    cancelled = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({slot, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cancelled.cancel()
        if not slot.done():
            slot.cancel()
        try:
            token = await slot
        except asyncio.CancelledError:
            token = None
    if token is not None and cancel_event.is_set():
        token.release()   # both came at once: the cancel wins
        return None
    return token


async def _check_one(
    check: Check, cancel_event: asyncio.Event | None,
    persist: Callable[[str, str], Awaitable[bool]] | None = None,
) -> tuple[str, str, bool, bool]:
    """Check one picked Image, if it is still the one picked.

    Returns ``(outcome, message, stop, written)``. First a slot on the
    ``verify`` lane, the one the Verify action uses. Only then the Image and its
    files are locked (:func:`_hold`), so nothing is held while the check queues,
    then compared with what the sweep picked (:func:`_unchanged`). Gone, changed
    or being written is ``SKIPPED``: no check time, no Finding, no
    notification. A verdict is written by *persist* while the locks are still
    held, so a writer cannot replace the file between the Verify and the record
    that describes it; the locks go once it is written, cancel included.
    """
    token = await _acquire_slot(cancel_event)
    if token is None:
        return CANCELLED, "", False, False
    async with token:
        held = await _take(check)
        if held is None:
            return SKIPPED, "", False, False
        if isinstance(held, _LockFailed):
            # No verdict, and nothing is locked: an attempt time only.
            written = await persist(INCONCLUSIVE, held.message) if persist else False
            return INCONCLUSIVE, held.message, False, written
        try:
            try:
                same = await asyncio.wait_for(
                    run_detached(_unchanged, check), _RECHECK_SECONDS,
                )
            except (asyncio.TimeoutError, OSError):
                same = False
            if not same:
                return SKIPPED, "", False, False
            verdict, message, stop = await _verify(check, cancel_event)
            written = False
            if verdict in (INTACT, NOT_INTACT, INCONCLUSIVE) and persist is not None:
                written = await persist(verdict, message)
            return verdict, message, stop, written
        finally:
            await _release(held)


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
