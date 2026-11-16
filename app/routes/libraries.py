"""Libraries and their Rules (#303).

``/api/libraries`` lists every Library (RomM, then each registered folder),
registers a folder as a Library and removes one. The Rules endpoints work for
any Library; RomM keeps its own ``/api/romm/rules`` routes as well, unchanged.

A Library's id goes in the path (``romm``, ``folder:3``). Rules are validated
against the Library's capabilities here, before they reach the engine: a
folder Library has no converted history, so a Rule asking for ``overwrite`` or
``rename`` is refused with 422 instead of being stored as something else.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from routes import rules as shared_rule_routes
from services import libraries
from services.libraries.folder import ListingInProgress
from services.rules import editor as rules_editor
from services.rules.library import Library, LibraryUnreadable
from services.subprocess_runner import run_detached
from services.tools import registry

router = APIRouter()

# The 409 detail for a listing that cannot start because the previous walk of
# the folder is still running. The Rules view shows it as a wait with a retry,
# not as an error.
LISTING_IN_PROGRESS = "listing_in_progress"

# How long a folder Library's walk may take before the request gives up. A
# healthy mount answers in seconds; a dead one would otherwise hold a request
# open forever.
_SCAN_SECONDS = 300
# How long a registration may take. It resolves the folder and RomM's library
# root, either of which may sit on a mount that stopped answering.
_REGISTER_SECONDS = 60


class RegisterLibraryRequest(BaseModel):
    path: str
    name: str | None = None


class UpdateLibraryRequest(BaseModel):
    name: str | None = None
    schedule_enabled: bool | None = None
    max_per_sweep: int | None = Field(default=None, ge=1)
    # Used by an EmulationStation frontend: conversions update gamelist.xml (#386).
    es_frontend: bool | None = None
    # Its ES-DE gamelists folder (#387): inside a Volume; "" clears it.
    gamelists_folder: str | None = None


class SweepRequest(BaseModel):
    """What a manual Preview / Run may ask for. ``limit`` only narrows."""

    platform_ids: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)


def _library(library_id: str) -> Library:
    library = libraries.get(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return library


@router.get("/libraries")
async def list_libraries() -> list[dict]:
    found = await run_in_threadpool(libraries.all_libraries)
    return [await run_in_threadpool(libraries.describe, lib) for lib in found]


@router.post("/libraries")
async def register_library(payload: RegisterLibraryRequest) -> dict:
    """Register a folder inside a Volume as a Library."""
    try:
        # A detached daemon thread under a bound, as walks run: a dead mount
        # strands that one thread, never a shared worker. Admission-controlled
        # (`libraries.run_admitted`): with every worker slot still held by an
        # earlier stuck registration, this answers 503 and starts nothing.
        library = await libraries.run_admitted(
            libraries.register, payload.path, payload.name, timeout=_REGISTER_SECONDS,
        )
    except libraries.LibraryPathError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                "The folder or RomM's library folder did not answer in time. The "
                "registration may still finish; list the Libraries before retrying"
            ),
        ) from None
    return await run_in_threadpool(libraries.describe, library)


@router.patch("/libraries/{library_id}")
async def update_library(library_id: str, payload: UpdateLibraryRequest) -> dict:
    """Rename a folder Library, or change its schedule switch, sweep limit,
    "used by an EmulationStation frontend" switch or ES-DE gamelists folder
    (inside a Volume, refused otherwise; ``""`` clears it).

    RomM's equivalents live in its own settings (``/api/romm/settings``).
    """
    if _library(library_id).kind != "folder":
        raise HTTPException(status_code=400, detail="Change RomM in its own settings")

    def save():
        return libraries.update(
            library_id, name=payload.name,
            schedule_enabled=payload.schedule_enabled,
            max_per_sweep=payload.max_per_sweep,
            es_frontend=payload.es_frontend,
            gamelists_folder=payload.gamelists_folder,
        )

    try:
        if payload.gamelists_folder and payload.gamelists_folder.strip():
            # Resolves a folder that may sit on a mount that stopped
            # answering: bounded, like a registration (#387).
            library = await libraries.admitted(save, what="The gamelists folder")
        else:
            library = await run_in_threadpool(save)
    except libraries.LibraryPathError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return await run_in_threadpool(libraries.describe, library)


@router.delete("/libraries/{library_id}")
async def remove_library(library_id: str) -> dict:
    """Remove a folder Library with its Rules and run state. No file is touched."""
    if _library(library_id).kind != "folder":
        raise HTTPException(status_code=400, detail="RomM cannot be removed")
    if not await libraries.remove(library_id):
        raise HTTPException(status_code=404, detail="Library not found")
    return {"removed": library_id}


@router.get("/libraries/{library_id}/platforms")
async def library_platforms(library_id: str) -> dict:
    """A folder Library's detected Platforms with Image counts.

    Also how many Images are Unsorted, Conflicted or unreadable. Each Platform
    carries ``tool_ids`` / ``mode_ids``, narrowed the same way RomM's platform
    list is, so the Rules editor offers only modes for that Platform.
    """
    library = _library(library_id)
    summarize = getattr(library, "summary", None)
    if summarize is None:
        raise HTTPException(status_code=400, detail="Use /api/romm/platforms for RomM")
    try:
        # A fresh walk: this is what the operator is looking at. Detection is
        # cached per file, so it costs a stat per file, and the sweep that
        # follows reuses it.
        summary = await asyncio.wait_for(
            run_detached(lambda: summarize(refresh=True)), _SCAN_SECONDS,
        )
    except ListingInProgress:
        raise HTTPException(status_code=409, detail=LISTING_IN_PROGRESS) from None
    except LibraryUnreadable as exc:
        raise HTTPException(status_code=409, detail=f"Library unreadable: {exc}") from None
    except (asyncio.TimeoutError, OSError):
        raise HTTPException(
            status_code=504, detail="The Library's folder did not answer in time",
        ) from None
    ready = await registry.ready_tool_ids()
    for platform in summary["platforms"]:
        platform["tool_ids"] = registry.narrow_to_platform(ready, platform["slug"])
        platform["mode_ids"] = registry.modes_for_platform(platform["slug"])
    return {
        "library": libraries.describe(library),
        **summary,
        # Which modes may run unattended, as /api/romm/status reports it, so
        # the editor offers the same targets for every Library.
        "automatable_modes": sorted(
            spec.mode for spec in registry.mode_specs()
            if registry.mode_is_automatable(spec.mode)
        ),
    }


@router.get("/libraries/{library_id}/rules")
async def get_library_rules(library_id: str) -> dict:
    """The Library's Rules and run state, with the schema the editor renders from."""
    return await rules_editor.editor_payload(_library(library_id))


@router.put("/libraries/{library_id}/rules")
async def put_library_rules(library_id: str, payload: dict) -> dict:
    """Replace the Library's Rules. 422 for a policy the Library cannot honour."""
    return await shared_rule_routes.save_rules(_library(library_id), payload)


@router.post("/libraries/{library_id}/rules/forget-converted")
async def forget_library_converted(library_id: str, payload: dict | None = None) -> dict:
    """Clear the converted history and failed holds, per Platform or all."""
    return await shared_rule_routes.forget_converted(_library(library_id), payload)


@router.post("/libraries/{library_id}/preview")
async def preview_library(library_id: str, payload: SweepRequest | None = None) -> dict:
    """What a sweep would queue now. Ignores the schedule and moves no clock."""
    payload = payload or SweepRequest()
    return await rules_editor.sweep_now(
        _library(library_id), platform_ids=payload.platform_ids, limit=payload.limit,
        dry_run=True,
    )


@router.post("/libraries/{library_id}/run")
async def run_library(library_id: str, payload: SweepRequest | None = None) -> dict:
    """Sweep now, queueing real jobs. Ignores the schedule, honours everything else."""
    payload = payload or SweepRequest()
    return await rules_editor.sweep_now(
        _library(library_id), platform_ids=payload.platform_ids, limit=payload.limit,
        dry_run=False,
    )
