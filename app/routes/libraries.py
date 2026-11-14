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
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from services import libraries
from services.rules import duplicate_actions
from services.rules import rules as rules_schema
from services.rules import sweep as rules_sweep
from services.rules.library import Library, LibraryUnreadable
from services.subprocess_runner import run_detached
from services.tools import registry

router = APIRouter()

# How long a folder Library's walk may take before the request gives up. A
# healthy mount answers in seconds; a dead one would otherwise hold a request
# open forever.
_SCAN_SECONDS = 120


class RegisterLibraryRequest(BaseModel):
    path: str
    name: str | None = None


class UpdateLibraryRequest(BaseModel):
    name: str | None = None
    schedule_enabled: bool | None = None
    max_per_sweep: int | None = Field(default=None, ge=1)


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
        library = await run_in_threadpool(libraries.register, payload.path, payload.name)
    except libraries.LibraryPathError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    return await run_in_threadpool(libraries.describe, library)


@router.patch("/libraries/{library_id}")
async def update_library(library_id: str, payload: UpdateLibraryRequest) -> dict:
    """Rename a folder Library, or change its schedule switch or sweep limit.

    RomM's equivalents live in its own settings (``/api/romm/settings``).
    """
    if _library(library_id).kind != "folder":
        raise HTTPException(status_code=400, detail="Change RomM in its own settings")
    library = await run_in_threadpool(
        lambda: libraries.update(
            library_id, name=payload.name,
            schedule_enabled=payload.schedule_enabled,
            max_per_sweep=payload.max_per_sweep,
        ),
    )
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
        summary = await asyncio.wait_for(run_detached(summarize), _SCAN_SECONDS)
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
    return {"library": libraries.describe(library), **summary}


@router.get("/libraries/{library_id}/rules")
async def get_library_rules(library_id: str) -> dict:
    """The Library's Rules and run state, with the schema the editor renders from."""
    library = _library(library_id)
    return {
        "rules": await rules_sweep.get_rules(library),
        "state": await rules_sweep.get_state(library),
        "defaults": rules_schema.default_rule(defaults=library.rule_defaults()),
        "options": {
            "orders": list(rules_schema.ORDERS),
            "duplicate_actions": list(duplicate_actions(library)),
            "days": list(rules_schema.ALL_DAYS),
        },
    }


def _refuse_unsupported_policies(library: Library, raw: Any) -> None:
    allowed = duplicate_actions(library)
    if not isinstance(raw, dict):
        return
    for platform_id, rule in raw.items():
        if not isinstance(rule, dict):
            continue
        policy = rule.get("duplicate_action")
        if policy is not None and policy not in allowed:
            raise HTTPException(status_code=422, detail={
                "error": "duplicate_action_unsupported",
                "platform_id": platform_id,
                "duplicate_action": policy,
                "allowed": list(allowed),
            })


@router.put("/libraries/{library_id}/rules")
async def put_library_rules(library_id: str, payload: dict) -> dict:
    """Replace the Library's Rules. Rules naming an unknown mode or Platform are dropped."""
    library = _library(library_id)
    raw = payload.get("rules", payload)
    _refuse_unsupported_policies(library, raw)
    return {"rules": await rules_sweep.set_rules(library, raw)}


@router.post("/libraries/{library_id}/rules/forget-converted")
async def forget_library_converted(library_id: str, payload: dict | None = None) -> dict:
    library = _library(library_id)
    ids = (payload or {}).get("platform_ids")
    cleared = await rules_sweep.forget_converted(
        library, [str(p) for p in ids] if ids is not None else None,
    )
    return {"cleared": cleared, "state": await rules_sweep.get_state(library)}


@router.post("/libraries/{library_id}/preview")
async def preview_library(library_id: str, payload: SweepRequest | None = None) -> dict:
    """What a sweep would queue now. Ignores the schedule and moves no clock."""
    library = _library(library_id)
    payload = payload or SweepRequest()
    return await rules_sweep.sweep(
        library, platform_ids=payload.platform_ids, ignore_schedule=True,
        dry_run=True, overall_limit=payload.limit,
    )


@router.post("/libraries/{library_id}/run")
async def run_library(library_id: str, payload: SweepRequest | None = None) -> dict:
    """Sweep now, queueing real jobs. Ignores the schedule, honours everything else."""
    library = _library(library_id)
    payload = payload or SweepRequest()
    return await rules_sweep.sweep(
        library, platform_ids=payload.platform_ids, ignore_schedule=True,
        dry_run=False, overall_limit=payload.limit,
    )
