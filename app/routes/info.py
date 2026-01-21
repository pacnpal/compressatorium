import os

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse
import json
import asyncio
import time
import contextlib

from models import CHDInfo
from services.chdman import chdman_service
from services.verification_store import verification_store
from utils.path_utils import is_within_configured_volumes

router = APIRouter()


@router.get("/info", response_model=CHDInfo)
async def get_chd_info(
    path: str = Query(..., description="Path to CHD file")
):
    """Get information about a CHD file."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        info = await chdman_service.info(path)

        return CHDInfo(
            file=path,
            input_file=info.get("input_file"),
            file_version=info.get("file_version"),
            logical_size=info.get("logical_size"),
            hunk_size=info.get("hunk_size"),
            total_hunks=info.get("total_hunks"),
            unit_size=info.get("unit_size"),
            total_units=info.get("total_units"),
            compression=info.get("compression"),
            chd_size=info.get("chd_size"),
            ratio=info.get("ratio"),
            sha1=info.get("sha1"),
            data_sha1=info.get("data_sha1"),
            raw_data=info.get("raw_data", "")
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read CHD info: {str(e)}")


@router.get("/verify")
async def verify_chd(
    path: str = Query(..., description="Path to CHD file to verify")
) -> dict:
    """Verify the integrity of a CHD file."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        result = await chdman_service.verify(path)
        if result.get("valid"):
            verification_store.mark_verified(path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify CHD: {str(e)}")


@router.get("/verify/events")
async def verify_chd_events(
    path: str = Query(..., description="Path to CHD file to verify")
) -> EventSourceResponse:
    """Stream CHD verification progress updates."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        done = asyncio.Event()
        start = time.monotonic()

        async def run_verify():
            try:
                async for update in chdman_service.verify_stream(path):
                    await queue.put(update)
            except Exception as exc:
                await queue.put({"type": "error", "valid": False, "message": str(exc)})
            finally:
                done.set()

        verify_task = asyncio.create_task(run_verify())
        try:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=2)
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - start)
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps({
                            "progress": None,
                            "message": f"Verifying... ({elapsed}s)"
                        })
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(update)
                    }
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        verification_store.mark_verified(path)
                    yield {
                        "event": "verify_complete",
                        "data": json.dumps(update)
                    }
                    break
                elif update.get("type") == "error":
                    yield {
                        "event": "verify_error",
                        "data": json.dumps(update)
                    }
                    break
        finally:
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_task

    return EventSourceResponse(event_generator())


@router.get("/verified")
async def list_verified() -> dict:
    """List verified CHD paths."""
    verification_store.prune_missing()
    verified = []
    for record in verification_store.all_records():
        chd_path = record.get("chd_path")
        if chd_path and is_within_configured_volumes(chd_path, treat_archives=False):
            verified.append(chd_path)
    return {"verified": verified}
