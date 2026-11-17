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
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from routes import rules as shared_rule_routes
from services import duplicates, libraries
from services.libraries.folder import ListingInProgress
from services.platform_detect import canonical_slug
from services.rules import editor as rules_editor
from services.rules.library import Library, LibraryUnreadable, PlacementUnknown
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
    # Where a Platform's Images go (#430): libraries.folder.LAYOUTS.
    layout: Literal["esde", "romm", "flat"] | None = None


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

    folder = payload.gamelists_folder
    try:
        if folder and folder.strip():
            # Only the read-only check runs under the abandonment bound: it
            # resolves a folder that may sit on a mount that stopped
            # answering. Nothing is saved unless it passes in time, so a check
            # abandoned at the bound never saves later (Codex
            # PRRT_kwDOQ9nULM64Jpv1), as RomM's settings route does.
            folder = await libraries.admitted(
                libraries.check_gamelists_folder, folder, what="The gamelists folder",
            )
        library = await run_in_threadpool(
            lambda: libraries.update(
                library_id, name=payload.name,
                schedule_enabled=payload.schedule_enabled,
                max_per_sweep=payload.max_per_sweep,
                es_frontend=payload.es_frontend,
                gamelists_folder=folder,
                layout=payload.layout,
            ),
        )
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


def _placements(library: Library) -> list[dict]:
    """Where each of *library*'s Platforms' Images go (#430). Blocking.

    One listing of the Platforms, handed to every row's ``placement`` (Codex
    PRRT_kwDOQ9nULM64es7O: RomM's catalog was fetched again per row). Each
    row carries the Library's own Platform id: two RomM platforms can share a
    slug, and both rows stay, each saying why it can't be placed (Codex
    PRRT_kwDOQ9nULM64es7J).
    """
    listed = library.platforms()
    out = []
    for platform in listed:
        slug = canonical_slug(platform.slug or platform.id)
        row = {
            "id": platform.id, "platform": slug, "name": platform.name,
            "folder": None, "exists": None, "error": None,
        }
        try:
            placed = library.placement(slug, known=listed)
        except PlacementUnknown as exc:
            row["error"] = str(exc)
        else:
            rel = os.path.relpath(placed.folder, library.root)
            row["folder"] = "" if rel == "." else rel.replace(os.sep, "/")
            row["exists"] = placed.exists
        out.append(row)
    return sorted(out, key=lambda r: (r["platform"] or "", r["id"]))


@router.get("/libraries/{library_id}/placements")
async def library_placements(library_id: str) -> dict:
    """Where new Images of each of the Library's Platforms would land (#430):
    the folder relative to the Library's root (``roms/ps2``), whether it
    exists yet, or why it can't be told. Any kind of Library."""
    library = _library(library_id)
    try:
        placements = await asyncio.wait_for(
            run_detached(lambda: _placements(library)), _SCAN_SECONDS,
        )
    except ListingInProgress:
        raise HTTPException(status_code=409, detail=LISTING_IN_PROGRESS) from None
    except LibraryUnreadable as exc:
        raise HTTPException(status_code=409, detail=f"Library unreadable: {exc}") from None
    except (asyncio.TimeoutError, OSError):
        raise HTTPException(
            status_code=504, detail="The Library's folder did not answer in time",
        ) from None
    return {
        "library_id": library.id,
        "layout": libraries.describe(library)["layout"],
        "placements": placements,
    }


@router.post("/libraries/{library_id}/find-duplicates")
async def find_duplicates(library_id: str) -> dict:
    """Start the Library's find-duplicates run (#388), as a background job.

    The run reads the content keys it is missing and records each group of
    Images that hold one disc as a ``duplicate`` Finding. Read-only: nothing is
    deleted. 409 while the Library's previous run is still running.
    """
    library = _library(library_id)
    try:
        job = duplicates.start(library)
    except duplicates.LibraryRemoving:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "library_being_removed",
                "message": "This Library is being removed",
            },
        ) from None
    except duplicates.AlreadyRunning as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "find_duplicates_running", "job_id": exc.job_id,
                "message": "This Library's find-duplicates run is still running",
            },
        ) from None
    return {"job_id": job.id, "library_id": library.id}


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
