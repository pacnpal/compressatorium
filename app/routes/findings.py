"""Findings and folder Platform mappings (#304).

``/api/findings`` lists what needs a person's decision (Platform conflicts and
Unsorted Images, from every folder Library's last walk), dismisses one, and
walks every folder Library again on request. ``/api/platform-mappings`` sets
or clears "this folder is PSP" on a folder inside a folder Library; the
Library is walked again right away so its Findings follow.

Nothing here moves, renames or deletes a file.
"""

from __future__ import annotations

import asyncio
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from pydantic import BaseModel
from routes.files import CONFIRM_DELETE_FILE, _require_action_confirmation
from services import (
    dat_rename,
    duplicate_cleanup,
    duplicates,
    findings,
    libraries,
    platform_detect,
    platform_mappings,
)
from services.libraries.folder import ListingInProgress
from services.rules.library import LibraryUnreadable
from services.subprocess_runner import run_detached

router = APIRouter()
logger = get_logger()

# How long one Library's walk may take before the request stops waiting. The
# walk itself runs on (and may strand) a detached thread, as #303's do.
_WALK_SECONDS = 300
# How long a mapping change may take. It resolves the folder on its mount.
_MAPPING_SECONDS = 60
# How long the Findings view waits to settle the hand-started Findings (it
# stats the files each one names) before it lists them as they are.
_SETTLE_SECONDS = 5
# Held by the settle that is running (`_settle_blocking`), across threads.
_settle_running = threading.Lock()


class FindingRef(BaseModel):
    """The cause fingerprint of the Finding as the user was shown it.

    With the id it names one Finding and one cause, so a row left on a screen
    after a walk changed or cleared it never acts on something else (409).
    """

    fingerprint: str


_CHANGED = "This Finding changed since it was listed; list the Findings again"


class MappingRequest(BaseModel):
    """A folder's absolute path, and its Platform slug (None clears the mapping)."""

    path: str
    platform: str | None = None


async def _walk(library) -> dict:
    """Walk one folder Library now, which records its Findings. Never raises."""
    try:
        await asyncio.wait_for(
            run_detached(lambda: library.summary(refresh=True)), _WALK_SECONDS,
        )
    except ListingInProgress:
        # The walk already running finishes first. If a mapping changed under
        # it, it drops its Findings and walks again (FolderLibrary.scan).
        return {
            "id": library.id, "name": library.name, "status": "listing_in_progress",
            "message": "A listing is running; Findings refresh after it.",
        }
    except LibraryUnreadable as exc:
        return {"id": library.id, "name": library.name, "status": exc.code, "error": str(exc)}
    except (asyncio.TimeoutError, OSError):
        return {"id": library.id, "name": library.name, "status": "library_unresponsive"}
    return {"id": library.id, "name": library.name, "status": "ok"}


def _settle_blocking() -> int:
    """One settle of the hand-started Findings, unless one is running."""
    if not _settle_running.acquire(blocking=False):
        return 0
    try:
        return findings.clear_gone(findings.NO_LIBRARY, findings.SOURCE_KEPT)
    finally:
        _settle_running.release()


async def _settle_started_by_hand() -> None:
    """Clear the hand-started Findings whose files are gone (#329). Never raises.

    They have no Library, so no sweep settles them; the view does, as it
    loads. Bounded and detached: a volume that does not answer leaves them
    listed as they are, and the next load tries again. At most one settle is
    in flight (CodeRabbit 4580360066): while one runs, even one stuck on a
    dead mount, a load starts none and lists the Findings as they are, so
    stuck settles never take more than one detached probe slot.
    """
    if _settle_running.locked():
        return
    try:
        await asyncio.wait_for(run_detached(_settle_blocking), _SETTLE_SECONDS)
    except (asyncio.TimeoutError, OSError):
        logger.warning(
            "Findings: checking the hand-started Findings did not finish; "
            "the next load tries again",
        )


async def _settle_rename_conflicts() -> None:
    """Clear the rename conflicts the ranking now resolves (#425). Never
    raises (CodeRabbit 4602644770): a settle that fails or runs past its
    bound (it may resolve a path on a Volume that stopped answering,
    CodeRabbit 4602644800) leaves them listed as they are, and the next load
    tries again. Detached and bounded, as the hand-started settle is."""
    try:
        await asyncio.wait_for(run_detached(dat_rename.settle_conflicts), _SETTLE_SECONDS)
    except Exception:
        logger.warning(
            "Findings: settling the rename conflicts did not finish; the next load tries again",
            exc_info=True,
        )


@router.get("/findings")
async def list_findings(include_dismissed: bool = False) -> dict:
    """Open Findings with their Library's name, by Library and Image."""
    await _settle_started_by_hand()
    await _settle_rename_conflicts()
    rows = await run_in_threadpool(
        lambda: findings.list_findings(include_dismissed=include_dismissed),
    )
    dismissed = await run_in_threadpool(findings.count_dismissed)
    names = {lib.id: lib.name for lib in await run_in_threadpool(libraries.all_libraries)}
    names[findings.NO_LIBRARY] = findings.NO_LIBRARY_NAME
    for row in rows:
        row["library_name"] = names.get(row["library_id"], row["library_id"])
        # The folder holding the Image: where "open the folder" goes, and the
        # folder a mapping would be set on.
        row["folder"] = os.path.dirname(row["path"])
    # Each Library's last find-duplicates run (#388): what it could not check
    # is shown beside its groups. A run of a Library since removed is gone.
    runs = [
        {**run, "library_name": names[run["library_id"]]}
        for run in await run_in_threadpool(duplicates.list_runs)
        if run["library_id"] in names
    ]
    # Each duplicate group's suggested keeper and whether a delete is offered
    # (#389), from the database only.
    await run_in_threadpool(duplicate_cleanup.annotate, rows)
    return {"findings": rows, "dismissed": dismissed, "duplicate_runs": runs}


def _library_is_current(library_id: str) -> bool:
    """Still a Library that is not being removed (#303's token, where it has one)."""
    library = libraries.get(library_id)
    if library is None:
        return False
    is_current = getattr(library, "is_current", None)
    return is_current() if is_current is not None else True


@router.post("/findings/{finding_id}/dismiss")
async def dismiss_finding(finding_id: int, payload: FindingRef) -> dict:
    """Hide a Finding until its cause changes. The Image is left as it is."""
    try:
        done = await run_in_threadpool(
            lambda: findings.dismiss(
                finding_id, fingerprint=payload.fingerprint, current=_library_is_current,
            ),
        )
    except findings.FindingChanged:
        raise HTTPException(status_code=409, detail=_CHANGED) from None
    if not done:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"dismissed": finding_id}


@router.post("/findings/{finding_id}/undismiss")
async def undismiss_finding(finding_id: int, payload: FindingRef) -> dict:
    """Show a dismissed Finding again."""
    try:
        done = await run_in_threadpool(
            lambda: findings.undismiss(finding_id, fingerprint=payload.fingerprint),
        )
    except findings.FindingChanged:
        raise HTTPException(status_code=409, detail=_CHANGED) from None
    if not done:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"undismissed": finding_id}


class DuplicateKeep(BaseModel):
    """A duplicate group as the user was shown it, and the copy to keep (#389)."""

    fingerprint: str
    keep: str


class DuplicateDelete(DuplicateKeep):
    """The confirmed delete: every path the plan listed, exactly."""

    delete_paths: list[str]


@router.post("/findings/{finding_id}/delete-plan")
async def duplicate_delete_plan(finding_id: int, payload: DuplicateKeep) -> dict:
    """What deleting every copy of a duplicate group but *keep* would remove.

    Read-only: the confirmation list. Each copy's delete plan (a sheet's
    tracks, a split set's parts) and the playlists that would be pointed at
    the copy kept. ``blocked`` when any copy is not the file the Finding
    showed, is a sheet's track, or would take the keeper with it.
    """
    try:
        return await duplicate_cleanup.plan(finding_id, payload.fingerprint, payload.keep)
    except duplicate_cleanup.CleanupRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@router.post("/findings/{finding_id}/delete")
async def duplicate_delete(finding_id: int, payload: DuplicateDelete, request: Request) -> dict:
    """Start deleting every copy but *keep*, after the user confirmed the plan (#389).

    Needs the delete confirmation header and the very paths the plan listed.
    Answers ``{job_id}`` at once: the delete is a background job (reading the
    Volumes' sheets again, making the plan again and checking every copy, the
    keeper's full read, then each copy through the job's own delete step under
    its locks), with progress, cancel and its outcome in ``result``. 409 when
    the group offers no delete or a delete of it is already running.
    """
    _require_action_confirmation(request, CONFIRM_DELETE_FILE, "delete action")
    try:
        return await duplicate_cleanup.start_delete(
            finding_id, payload.fingerprint, payload.keep, payload.delete_paths,
        )
    except duplicate_cleanup.CleanupRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@router.post("/findings/check")
async def check_findings() -> dict:
    """Walk every folder Library now, side by side, so its Findings are current."""
    found = await run_in_threadpool(libraries.folder_libraries)
    return {"libraries": list(await asyncio.gather(*(_walk(lib) for lib in found)))}


@router.get("/platforms")
def list_platforms() -> list[dict]:
    """Every Platform a folder can be set to, by display name."""
    return platform_detect.platforms()


@router.get("/platform-mappings")
async def list_platform_mappings() -> list[dict]:
    return await run_in_threadpool(platform_mappings.mappings)


@router.put("/platform-mappings")
async def set_platform_mapping(payload: MappingRequest) -> dict:
    """Set, or with ``platform: null`` clear, the Platform of a folder in a Library."""
    try:
        # Admission-controlled, as registration is (#303's
        # `libraries.run_admitted`): the save resolves and stats a folder on a
        # mount that may have stopped answering. Its worker keeps a slot until
        # it actually returns, and with every slot held this answers 503 and
        # starts nothing, so retries cannot use up the shared probe ceiling.
        saved = await libraries.run_admitted(
            platform_mappings.set_mapping, payload.path, payload.platform,
            timeout=_MAPPING_SECONDS,
        )
    except platform_mappings.MappingError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except libraries.LibraryPathError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except asyncio.TimeoutError:
        # The save runs on a detached thread and may still land: say so.
        raise HTTPException(
            status_code=504,
            detail="The folder did not answer in time; the Platform may still be saved",
        ) from None
    library = libraries.get(saved["library_id"])
    if library is not None:
        saved["walk"] = await _walk(library)
    return saved
