import os
import logging

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from sse_starlette.sse import EventSourceResponse
import json
import asyncio
import time
import contextlib

from config import settings
from models import CHDInfo, BulkVerifyRequest, MetadataBatchRequest
from services.chdman import chdman_service
from services.verification_store import verification_store
from services.chd_metadata_store import chd_metadata_store
from utils.path_utils import is_within_configured_volumes

logger = logging.getLogger("chd")
router = APIRouter()

# Lock for scanning to prevent concurrent scans
_scan_lock = asyncio.Lock()
_is_scanning = False


async def scan_metadata_task():
    """Background task to scan all volumes for missing CHD metadata."""
    global _is_scanning
    # Note: _is_scanning is already set to True by the trigger endpoint
    logger.info("Starting background metadata scan...")
    count = 0
    
    def collect_stale_chd_paths():
        """Collect CHD paths that need metadata refresh (runs in thread pool)."""
        stale_paths = []
        for volume in settings.volumes:
            if not os.path.exists(volume):
                continue
            for root, _, files in os.walk(volume):
                for file in files:
                    if file.lower().endswith(".chd"):
                        path = os.path.join(root, file)
                        if chd_metadata_store.is_stale(path):
                            stale_paths.append(path)
        return stale_paths
    
    try:
        # Run blocking filesystem traversal in thread pool
        loop = asyncio.get_event_loop()
        stale_paths = await loop.run_in_executor(None, collect_stale_chd_paths)
        logger.info(f"Found {len(stale_paths)} CHD files needing metadata refresh")
        
        # Process each path (chdman_service.info is already async)
        for path in stale_paths:
            try:
                info = await chdman_service.info(path)
                chd_metadata_store.set_metadata(path, info, persist=False)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to scan metadata for {path}: {e}")
        
        # Flush all accumulated changes once at the end (async, non-blocking)
        await chd_metadata_store.flush_async()
        
    except Exception as e:
        logger.error(f"Metadata scan failed: {e}")
    finally:
        _is_scanning = False
        logger.info(f"Metadata scan complete. Updated {count} files.")


@router.get("/version")
async def get_app_version() -> dict:
    """Get the application version."""
    # Use importlib for reliable import regardless of how app is started
    import importlib
    try:
        main_module = importlib.import_module("main")
    except ModuleNotFoundError:
        main_module = importlib.import_module("app.main")
    return {"version": main_module.get_version()}



@router.get("/info", response_model=CHDInfo)
async def get_chd_info(path: str = Query(..., description="Path to CHD file")):
    """Get information about a CHD file (cached with mtime-based invalidation)."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        # Check cache first
        cached_info = None
        if not chd_metadata_store.is_stale(path):
            cached_info = chd_metadata_store.get_metadata(path)
        
        if cached_info:
            info = cached_info
            media_type = chd_metadata_store.get_media_type(path)
        else:
            # Run chdman info and cache the result (non-blocking persist)
            info = await chdman_service.info(path)
            record = chd_metadata_store.set_metadata(path, info, persist=False)
            
            # Schedule async flush in background with error handling
            async def safe_flush():
                try:
                    await chd_metadata_store.flush_async()
                except Exception as e:
                    logger.warning(f"Background metadata flush failed: {e}")
            
            asyncio.create_task(safe_flush())
            media_type = record.get("media_type")

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
            raw_data=info.get("raw_data", ""),
            media_type=media_type,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to read CHD info: {str(e)}"
        )


@router.post("/chd-metadata")
async def get_chd_metadata_batch(
    request: MetadataBatchRequest,
) -> dict:
    """
    Get cached metadata for multiple CHD files.
    
    Returns media_type for each path that has cached metadata.
    Does NOT run chdman info - only returns already-cached data.
    """
    result = {}
    
    for path in request.paths:
        if not is_within_configured_volumes(path, treat_archives=False):
            continue
        if not os.path.isfile(path):
            continue
        if not path.lower().endswith(".chd"):
            continue
        
        # Only return cached data, don't run chdman info
        if not chd_metadata_store.is_stale(path):
            media_type = chd_metadata_store.get_media_type(path)
            result[path] = {"media_type": media_type, "cached": True}
        else:
            # Not cached yet - frontend can trigger individual info calls
            result[path] = {"media_type": None, "cached": False}
    
    return result


@router.post("/chd-metadata/scan")
async def trigger_metadata_scan(background_tasks: BackgroundTasks):
    """Trigger a background scan of all volumes for missing CHD metadata."""
    global _is_scanning
    
    # Use lock to prevent race condition between check and set
    async with _scan_lock:
        if _is_scanning:
            return {"status": "scanning", "message": "Scan already in progress"}
        _is_scanning = True
    
    background_tasks.add_task(scan_metadata_task)
    return {"status": "started", "message": "Metadata scan started in background"}


@router.get("/chd-metadata/scan/status")
async def get_scan_status():
    """Get the status of the metadata scan."""
    return {"scanning": _is_scanning}


@router.get("/verify")
async def verify_chd(
    path: str = Query(..., description="Path to CHD file to verify"),
) -> dict:
    """Verify the integrity of a CHD file."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

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
    path: str = Query(..., description="Path to CHD file to verify"),
) -> EventSourceResponse:
    """Stream CHD verification progress updates."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

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
                        "data": json.dumps(
                            {"progress": None, "message": f"Verifying... ({elapsed}s)"}
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {"event": "verify_progress", "data": json.dumps(update)}
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        verification_store.mark_verified(path)
                    yield {"event": "verify_complete", "data": json.dumps(update)}
                    break
                elif update.get("type") == "error":
                    yield {"event": "verify_error", "data": json.dumps(update)}
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


@router.post("/verify-batch/events")
async def verify_batch_events(request: BulkVerifyRequest) -> EventSourceResponse:
    """Stream verification progress for multiple CHD files."""
    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    # Validate all paths upfront
    valid_paths = []
    for path in request.paths:
        if not is_within_configured_volumes(path, treat_archives=False):
            continue
        if not os.path.isfile(path):
            continue
        if not path.lower().endswith(".chd"):
            continue
        valid_paths.append(path)

    async def event_generator():
        total = len(valid_paths)
        verified_count = 0
        failed_count = 0

        # Send initial status
        yield {
            "event": "verify_batch_start",
            "data": json.dumps({"total": total, "paths": valid_paths}),
        }

        for idx, path in enumerate(valid_paths):
            filename = os.path.basename(path)
            start = time.monotonic()

            # Send file start event
            yield {
                "event": "verify_batch_progress",
                "data": json.dumps(
                    {
                        "index": idx,
                        "total": total,
                        "path": path,
                        "filename": filename,
                        "status": "verifying",
                        "verified": verified_count,
                        "failed": failed_count,
                    }
                ),
            }

            try:
                # Use the streaming verify to get progress updates
                queue: asyncio.Queue = asyncio.Queue()
                done = asyncio.Event()
                final_result = {"valid": False, "message": "Unknown error"}

                async def run_verify():
                    nonlocal final_result
                    try:
                        async for update in chdman_service.verify_stream(path):
                            await queue.put(update)
                            if update.get("type") in ("complete", "error"):
                                final_result = update
                    except Exception as exc:
                        final_result = {
                            "type": "error",
                            "valid": False,
                            "message": str(exc),
                        }
                        await queue.put(final_result)
                    finally:
                        done.set()

                verify_task = asyncio.create_task(run_verify())
                try:
                    while not done.is_set() or not queue.empty():
                        try:
                            update = await asyncio.wait_for(queue.get(), timeout=2)
                            if update.get("type") == "progress":
                                yield {
                                    "event": "verify_batch_file_progress",
                                    "data": json.dumps(
                                        {
                                            "index": idx,
                                            "path": path,
                                            "filename": filename,
                                            "progress": update.get("progress"),
                                            "message": update.get("message"),
                                        }
                                    ),
                                }
                            elif update.get("type") in ("complete", "error"):
                                break
                        except asyncio.TimeoutError:
                            elapsed = int(time.monotonic() - start)
                            yield {
                                "event": "verify_batch_file_progress",
                                "data": json.dumps(
                                    {
                                        "index": idx,
                                        "path": path,
                                        "filename": filename,
                                        "progress": None,
                                        "message": f"Verifying... ({elapsed}s)",
                                    }
                                ),
                            }
                finally:
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await verify_task

                if final_result.get("valid"):
                    verification_store.mark_verified(path)
                    verified_count += 1
                    status = "verified"
                else:
                    failed_count += 1
                    status = "failed"

                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(
                        {
                            "index": idx,
                            "path": path,
                            "filename": filename,
                            "status": status,
                            "valid": final_result.get("valid", False),
                            "message": final_result.get("message"),
                            "verified": verified_count,
                            "failed": failed_count,
                        }
                    ),
                }

            except Exception as exc:
                failed_count += 1
                yield {
                    "event": "verify_batch_file_complete",
                    "data": json.dumps(
                        {
                            "index": idx,
                            "path": path,
                            "filename": filename,
                            "status": "failed",
                            "valid": False,
                            "message": str(exc),
                            "verified": verified_count,
                            "failed": failed_count,
                        }
                    ),
                }

        # Send final completion event
        yield {
            "event": "verify_batch_complete",
            "data": json.dumps(
                {"total": total, "verified": verified_count, "failed": failed_count}
            ),
        }

    return EventSourceResponse(event_generator())
