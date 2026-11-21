"""The *arr webhook and "scan this path now" (#522).

``POST /api/hooks/arr`` takes Sonarr, Radarr and ROMarr webhook JSON;
``POST /api/scan`` takes ``{"path": ...}`` or ``{"paths": [...]}`` from a
script. Both need the app's auth (when it is on) and the action confirmation
header ``X-Chd-Action-Confirm: scan``, the ``Test`` event included, so an *arr's
Test button proves the whole setup. The work is :mod:`services.inbox.trigger`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from routes.files import _require_action_confirmation
from services.inbox import trigger

from config import settings

router = APIRouter()

CONFIRM_SCAN = "scan"
MAX_BODY_BYTES = 1024 * 1024


async def _body(request: Request) -> dict[str, Any]:
    _require_action_confirmation(request, CONFIRM_SCAN, "scan")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="The body is too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="The body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="The body must be a JSON object")
    return body


async def _take(body: dict[str, Any], source: str) -> JSONResponse:
    paths = trigger.paths_in(body)
    if not paths:
        raise HTTPException(status_code=422, detail="No file path in the body")
    result = await trigger.process(paths, source=source)
    return JSONResponse(result, status_code=202 if result["accepted"] else 200)


@router.post("/hooks/arr")
async def arr_webhook(request: Request) -> Any:
    """A Sonarr, Radarr or ROMarr webhook. ``Test`` answers ok; an import
    (``Download``/``Import``) takes each file it names into the Inbox that
    holds it, now; other events are acknowledged and ignored."""
    body = await _body(request)
    event = str(body.get("eventType") or "")
    if event.lower() == "test":
        return {"ok": True, "test": True}
    if not trigger.imports(body):
        return {"ok": True, "ignored": event}
    return await _take(body, event or "webhook")


@router.post("/scan")
async def scan_path(request: Request) -> Any:
    """Take the file or folder at ``path`` (or each of ``paths``) into the
    Inbox that holds it, now, skipping the settle wait."""
    return await _take(await _body(request), "scan")


@router.get("/hooks/arr")
async def arr_settings() -> dict:
    """What the setup card shows: the rewrites, the paths received lately,
    and what an *arr must send."""
    return {
        **await trigger.overview(),
        "auth": bool(settings.enable_auth),
        "username": settings.auth_username if settings.enable_auth else None,
        "confirm_header": {"name": "X-Chd-Action-Confirm", "value": CONFIRM_SCAN},
    }


@router.put("/hooks/arr/rewrites")
async def put_rewrites(payload: dict) -> dict:
    """Replace the path rewrites (``[{"from", "to"}]``)."""
    try:
        return {"rewrites": await trigger.save_rewrites(payload.get("rewrites"))}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/hooks/arr/check")
async def check_path(payload: dict) -> dict:
    """Where a path as the *arr sees it lands here, which Inbox takes it, or
    why none would, with a rewrite to add. Touches no file."""
    path = str(payload.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=422, detail="Give a path")
    return await trigger.check_one(path)
