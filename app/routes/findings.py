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

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from services import findings, libraries, platform_detect, platform_mappings
from services.libraries.folder import ListingInProgress
from services.rules.library import LibraryUnreadable
from services.subprocess_runner import run_detached

router = APIRouter()

# How long one Library's walk may take before the request stops waiting. The
# walk itself runs on (and may strand) a detached thread, as #303's do.
_WALK_SECONDS = 300
# How long a mapping change may take. It resolves the folder on its mount.
_MAPPING_SECONDS = 60


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
            "id": library.id, "status": "listing_in_progress",
            "message": "A listing is running; Findings refresh after it.",
        }
    except LibraryUnreadable as exc:
        return {"id": library.id, "status": exc.code, "error": str(exc)}
    except (asyncio.TimeoutError, OSError):
        return {"id": library.id, "status": "library_unresponsive"}
    return {"id": library.id, "status": "ok"}


@router.get("/findings")
async def list_findings(include_dismissed: bool = False) -> dict:
    """Open Findings with their Library's name, by Library and Image."""
    rows = await run_in_threadpool(
        lambda: findings.list_findings(include_dismissed=include_dismissed),
    )
    dismissed = await run_in_threadpool(findings.count_dismissed)
    names = {lib.id: lib.name for lib in await run_in_threadpool(libraries.all_libraries)}
    for row in rows:
        row["library_name"] = names.get(row["library_id"], row["library_id"])
        # The folder holding the Image: where "open the folder" goes, and the
        # folder a mapping would be set on.
        row["folder"] = os.path.dirname(row["path"])
    return {"findings": rows, "dismissed": dismissed}


@router.post("/findings/{finding_id}/dismiss")
async def dismiss_finding(finding_id: int) -> dict:
    """Hide a Finding until its cause changes. The Image is left as it is."""
    if not await run_in_threadpool(findings.dismiss, finding_id):
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"dismissed": finding_id}


@router.post("/findings/{finding_id}/undismiss")
async def undismiss_finding(finding_id: int) -> dict:
    """Show a dismissed Finding again."""
    if not await run_in_threadpool(findings.undismiss, finding_id):
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"undismissed": finding_id}


@router.post("/findings/check")
async def check_findings() -> dict:
    """Walk every folder Library now, so its Findings are current."""
    found = await run_in_threadpool(libraries.folder_libraries)
    return {"libraries": [await _walk(library) for library in found]}


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
        saved = await asyncio.wait_for(
            run_detached(platform_mappings.set_mapping, payload.path, payload.platform),
            _MAPPING_SECONDS,
        )
    except platform_mappings.MappingError as exc:
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
