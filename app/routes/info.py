import asyncio
import contextlib
import json
import logging
import os
import time

from config import settings
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from models import BulkVerifyRequest, CHDInfo, DolphinDiscInfo, MetadataBatchRequest, Z3DSInfo
from services.chd_metadata_store import chd_metadata_store
from services.chdman import chdman_service
from services.disc_id import (
    ensure_disc_id_embedded as disc_id_ensure_embedded,
    extract_from_chd as disc_id_extract_from_chd,
)
from services.dolphin_tool import (
    DOLPHIN_CONVERTIBLE_EXTENSIONS,
    dolphin_tool_service,
)
from services.workload_limiter import WorkloadToken, workload_limiter
from services.z3ds_compress import Z3DS_CONVERTIBLE_EXTENSIONS, z3ds_compress_service
from services.verification_store import verification_store
from sse_starlette.sse import EventSourceResponse
from utils.path_utils import is_within_configured_volumes

DOLPHIN_INFO_EXTENSIONS = DOLPHIN_CONVERTIBLE_EXTENSIONS

logger = logging.getLogger("chd")
router = APIRouter()

# Lock for scanning to prevent concurrent scans
_scan_lock = asyncio.Lock()
_is_scanning = False


def _verification_backpressure_detail() -> str:
    in_use = workload_limiter.in_use("verify")
    limit = workload_limiter.limit("verify")
    return (
        "Verification lane is at capacity "
        f"({in_use}/{limit}). Retry later."
    )


async def _acquire_verify_lane_or_429() -> WorkloadToken:
    token = await workload_limiter.try_acquire("verify")
    if token is None:
        raise HTTPException(status_code=429, detail=_verification_backpressure_detail())
    return token


async def scan_metadata_task(
    force: bool = False,
    lane_token: WorkloadToken | None = None,
):
    """Background task to scan all volumes for missing CHD metadata."""
    global _is_scanning
    # Note: _is_scanning is already set to True by the trigger endpoint
    logger.info("Starting background metadata scan...")
    count = 0
    scan_token = lane_token
    if scan_token is None:
        scan_token = await workload_limiter.acquire("metadata_scan")
    _is_scanning = True

    def collect_all_chd_paths():
        """Collect ALL CHD paths from volumes (runs in thread pool)."""
        paths = []
        for volume in settings.volumes:
            if not os.path.exists(volume):
                continue
            for root, _, files in os.walk(volume):
                for file in files:
                    if file.lower().endswith(".chd"):
                        paths.append(os.path.join(root, file))
        return paths

    try:
        # Run blocking filesystem traversal in thread pool
        loop = asyncio.get_running_loop()
        all_paths = await loop.run_in_executor(None, collect_all_chd_paths)

        # Filter stales asynchronously
        chd_paths = []
        for path in all_paths:
            if force or await chd_metadata_store.is_stale(path):
                chd_paths.append(path)

        if force:
            logger.info(f"Found {len(chd_paths)} CHD files for forced metadata refresh")
        else:
            logger.info(f"Found {len(chd_paths)} CHD files needing metadata refresh")

        # Phase 1: Update chdman info cache for stale / forced CHDs
        for path in chd_paths:
            try:
                info = await chdman_service.info(path)
                await chd_metadata_store.set_metadata(path, info, persist=False)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to scan metadata for {path}: {e}")

        # Phase 2: Retroactively embed GAME / NAME tags in any CHD that lacks
        # them.  Covers CHDs created before conversion-time tagging was added.
        # Skip CHDs where disc ID has already been checked and the file has not
        # changed since — avoids spawning a chdman subprocess on every scan.
        embed_count = 0
        for path in all_paths:
            try:
                if await chd_metadata_store.is_disc_id_checked(path):
                    continue
                result = await disc_id_ensure_embedded(path, settings.chdman_path)
                await chd_metadata_store.mark_disc_id_checked(path)
                if result:
                    embed_count += 1
            except Exception as e:
                logger.debug(f"disc_id ensure skipped for {path}: {e}")

        if embed_count:
            logger.info(
                f"Disc ID scan: ensured GAME/NAME tags for {embed_count} CHD file(s)"
            )

        # Flush all accumulated changes once at the end (async, non-blocking)
        await chd_metadata_store.flush_async()

    except Exception as e:
        logger.error(f"Metadata scan failed: {e}")
    finally:
        if scan_token:
            scan_token.release()
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
    return {
        "version": main_module.get_version(),
        "search_auto_return_to_file_list": settings.search_auto_return_to_file_list,
    }


# ============ Z3DS endpoints ============


Z3DS_VERIFY_EXTENSIONS = {".z3ds", ".zcci", ".zcia"}
Z3DS_INFO_EXTENSIONS = Z3DS_CONVERTIBLE_EXTENSIONS | Z3DS_VERIFY_EXTENSIONS


def _is_z3ds_info_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in Z3DS_INFO_EXTENSIONS


def _is_z3ds_verify_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in Z3DS_VERIFY_EXTENSIONS


@router.post("/z3ds-verify-batch/events")
async def verify_z3ds_batch_events(
    request: BulkVerifyRequest,
) -> EventSourceResponse:
    """Stream verification progress for multiple 3DS files."""
    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    # Validate all paths upfront
    valid_paths = []
    for path in request.paths:
        if not await run_in_threadpool(
            is_within_configured_volumes, path, treat_archives=False
        ):
            continue
        if not await run_in_threadpool(os.path.isfile, path):
            continue
        if not _is_z3ds_verify_file(path):
            continue
        valid_paths.append(path)

    verify_token = await _acquire_verify_lane_or_429()

    async def event_generator():
        try:
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
                    queue: asyncio.Queue = asyncio.Queue()
                    done = asyncio.Event()
                    final_result = {"valid": False, "message": "Unknown error"}

                    async def run_verify():
                        nonlocal final_result
                        try:
                            async for update in z3ds_compress_service.verify_stream(path):
                                if update.get("type") in ("complete", "error"):
                                    final_result = update
                                await queue.put(update)
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
                                    final_result = update
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
                        await verification_store.mark_verified(path)
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
        finally:
            verify_token.release()

    return EventSourceResponse(event_generator())


@router.get("/z3ds-verify")
async def verify_z3ds(
    path: str = Query(
        ..., description="Path to compressed 3DS ROM file to verify",
    ),
) -> dict:
    """Verify the integrity of a compressed 3DS ROM file."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_z3ds_verify_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported compressed 3DS format (.z3ds, .zcci, .zcia)",
        )

    verify_token = await _acquire_verify_lane_or_429()
    try:
        result = await z3ds_compress_service.verify(path)
        if result.get("valid"):
            await verification_store.mark_verified(path)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to verify 3DS ROM: {e!s}",
        )
    finally:
        verify_token.release()


@router.get("/z3ds-verify/events")
async def verify_z3ds_events(
    path: str = Query(
        ..., description="Path to compressed 3DS ROM file to verify",
    ),
) -> EventSourceResponse:
    """Stream 3DS ROM verification progress updates."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_z3ds_verify_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported compressed 3DS format (.z3ds, .zcci, .zcia)",
        )

    async def event_generator():
        verify_token = await workload_limiter.try_acquire("verify")
        if verify_token is None:
            yield {
                "event": "verify_error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "valid": False,
                        "message": _verification_backpressure_detail(),
                    },
                ),
            }
            return

        queue: asyncio.Queue = asyncio.Queue()
        done = asyncio.Event()
        start = time.monotonic()

        async def run_verify():
            try:
                async for update in z3ds_compress_service.verify_stream(path):
                    await queue.put(update)
            except Exception as exc:
                await queue.put(
                    {"type": "error", "valid": False, "message": str(exc)},
                )
            finally:
                done.set()

        verify_task = asyncio.create_task(run_verify())
        try:
            while True:
                try:
                    update = await asyncio.wait_for(
                        queue.get(), timeout=2,
                    )
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - start)
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(
                            {
                                "progress": None,
                                "message": f"Verifying... ({elapsed}s)",
                            },
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(update),
                    }
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        await verification_store.mark_verified(path)
                    yield {
                        "event": "verify_complete",
                        "data": json.dumps(update),
                    }
                    break
                elif update.get("type") == "error":
                    yield {
                        "event": "verify_error",
                        "data": json.dumps(update),
                    }
                    break
        finally:
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_task
            verify_token.release()

    return EventSourceResponse(event_generator())


@router.get("/info", response_model=CHDInfo)
async def get_chd_info(path: str = Query(..., description="Path to CHD file")):
    """Get information about a CHD file (cached with mtime-based invalidation)."""
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        # Check cache first
        cached_info = None
        if not await chd_metadata_store.is_stale(path):
            cached_info, cached_media_type = await chd_metadata_store.get_full_info(path)

        if cached_info:
            info = cached_info
            media_type = cached_media_type
        else:
            # Run chdman info and cache the result (non-blocking persist)
            info = await chdman_service.info(path)
            record = await chd_metadata_store.set_metadata(path, info, persist=False)

            # Schedule async flush in background with error handling
            async def safe_flush():
                try:
                    await chd_metadata_store.flush_async()
                except Exception as e:
                    logger.warning(f"Background metadata flush failed: {e}")

            # Use BackgroundTasks if available, but here we are in a route without it passed explicitly for flush
            # We can spawn a task
            asyncio.create_task(safe_flush())
            media_type = record.get("media_type")

        # Extract game ID / title.  Prefer the cached value in the metadata
        # store (written by Phase 2 of the scan or by a prior /api/info call)
        # to avoid spawning chdman subprocesses on every request.
        cached_game_id, cached_title = await chd_metadata_store.get_disc_id_info(path)
        if cached_game_id is not None:
            disc_info: dict = {"game_id": cached_game_id}
            if cached_title is not None:
                disc_info["title"] = cached_title
        else:
            disc_info = {}
            try:
                disc_info = await disc_id_extract_from_chd(path, settings.chdman_path) or {}
            except Exception as e:
                logger.debug("disc_id extraction failed for %s: %s", path, e)
            if disc_info.get("game_id") is not None:
                await chd_metadata_store.update_disc_id_info(
                    path, disc_info.get("game_id"), disc_info.get("title")
                )

        game_id = disc_info.get("game_id")
        # Only surface a distinct human-readable title; skip when it equals
        # the serial (e.g. PS2/PS1 CHDs where we wrote serial as both tags).
        raw_title = disc_info.get("title")
        title = raw_title if raw_title and raw_title != game_id else None

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
            game_id=game_id,
            title=title,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to read CHD info: {e!s}",
        )


@router.post("/chd-metadata")
async def get_chd_metadata_batch(
    request: MetadataBatchRequest,
) -> dict:
    """Get cached metadata for multiple CHD files.
    
    For every requested path, an entry is returned. Invalid or non-CHD paths
    will have media_type=None, cached=False, and an error field.
    """
    result = {}

    for path in request.paths:
        # Default entry for each requested path
        entry = {"media_type": None, "cached": False}

        if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
            entry["error"] = "path_outside_configured_volumes"
            result[path] = entry
            continue

        if not await run_in_threadpool(os.path.isfile, path):
            entry["error"] = "file_not_found"
            result[path] = entry
            continue

        if not path.lower().endswith(".chd"):
            entry["error"] = "not_a_chd_file"
            result[path] = entry
            continue

        # Only return cached data, don't run chdman info
        if not await chd_metadata_store.is_stale(path):
            # Optimize: use get_full_info to avoid extra lock/lookup
            _, media_type = await chd_metadata_store.get_full_info(path)
            entry["media_type"] = media_type
            entry["cached"] = True
            # no error field for successful, cached entries
        else:
            # Not cached yet - frontend can trigger individual info calls
            entry["error"] = "not_cached"

        result[path] = entry

    return result


@router.post("/chd-metadata/scan")
async def trigger_metadata_scan(
    background_tasks: BackgroundTasks,
    force: bool = Query(False, description="Rescan all CHD files, ignoring cache"),
):
    """Trigger a background scan of all volumes for missing CHD metadata."""
    global _is_scanning

    # Use lock to prevent race condition between check and set
    async with _scan_lock:
        if _is_scanning:
            return {"status": "scanning", "message": "Scan already in progress"}
        scan_token = await workload_limiter.try_acquire("metadata_scan")
        if scan_token is None:
            return {"status": "scanning", "message": "Scan already in progress"}
        _is_scanning = True

    background_tasks.add_task(scan_metadata_task, force, scan_token)
    if force:
        return {
            "status": "started",
            "message": "Forced metadata scan started in background",
        }
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
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    verify_token = await _acquire_verify_lane_or_429()
    try:
        result = await chdman_service.verify(path)
        if result.get("valid"):
            await verification_store.mark_verified(path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify CHD: {e!s}")
    finally:
        verify_token.release()


@router.get("/verify/events")
async def verify_chd_events(
    path: str = Query(..., description="Path to CHD file to verify"),
) -> EventSourceResponse:
    """Stream CHD verification progress updates."""
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    async def event_generator():
        verify_token = await workload_limiter.try_acquire("verify")
        if verify_token is None:
            yield {
                "event": "verify_error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "valid": False,
                        "message": _verification_backpressure_detail(),
                    },
                ),
            }
            return

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
                            {"progress": None, "message": f"Verifying... ({elapsed}s)"},
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {"event": "verify_progress", "data": json.dumps(update)}
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        await verification_store.mark_verified(path)
                    yield {"event": "verify_complete", "data": json.dumps(update)}
                    break
                elif update.get("type") == "error":
                    yield {"event": "verify_error", "data": json.dumps(update)}
                    break
        finally:
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_task
            verify_token.release()

    return EventSourceResponse(event_generator())


@router.get("/verified")
async def list_verified() -> dict:
    """List verified output paths."""
    await verification_store.prune_missing()
    verified = []
    for record in verification_store.all_records():
        chd_path = record.get("chd_path")
        if chd_path and await run_in_threadpool(is_within_configured_volumes, chd_path, treat_archives=False):
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
        if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
            continue
        if not await run_in_threadpool(os.path.isfile, path):
            continue
        if not path.lower().endswith(".chd"):
            continue
        valid_paths.append(path)

    verify_token = await _acquire_verify_lane_or_429()

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
                    },
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
                                        },
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
                                    },
                                ),
                            }
                finally:
                    verify_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await verify_task

                if final_result.get("valid"):
                    await verification_store.mark_verified(path)
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
                        },
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
                        },
                    ),
                }

        # Send final completion event
        yield {
            "event": "verify_batch_complete",
            "data": json.dumps(
                {"total": total, "verified": verified_count, "failed": failed_count},
            ),
        }

    async def wrapped_event_generator():
        try:
            async for event in event_generator():
                yield event
        finally:
            verify_token.release()

    return EventSourceResponse(wrapped_event_generator())


# ============ Dolphin disc image endpoints ============


def _is_dolphin_file(path: str) -> bool:
    from pathlib import Path as _P
    return _P(path).suffix.lower() in DOLPHIN_INFO_EXTENSIONS


@router.post("/dolphin-verify-batch/events")
async def verify_dolphin_batch_events(
    request: BulkVerifyRequest,
) -> EventSourceResponse:
    """Stream verification progress for multiple disc images."""
    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    # Validate all paths upfront
    valid_paths = []
    for path in request.paths:
        if not await run_in_threadpool(
            is_within_configured_volumes, path, treat_archives=False
        ):
            continue
        if not await run_in_threadpool(os.path.isfile, path):
            continue
        if not _is_dolphin_file(path):
            continue
        valid_paths.append(path)

    verify_token = await _acquire_verify_lane_or_429()

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
                queue: asyncio.Queue = asyncio.Queue()
                done = asyncio.Event()
                final_result = {"valid": False, "message": "Unknown error"}

                async def run_verify():
                    nonlocal final_result
                    try:
                        async for update in dolphin_tool_service.verify_stream(path):
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
                    await verification_store.mark_verified(path)
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

    async def wrapped_event_generator():
        try:
            async for event in event_generator():
                yield event
        finally:
            verify_token.release()

    return EventSourceResponse(wrapped_event_generator())


@router.get("/dolphin-info", response_model=DolphinDiscInfo)
async def get_dolphin_info(
    path: str = Query(..., description="Path to disc image"),
):
    """Get header information about a GameCube/Wii disc image."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_dolphin_file(path):
        raise HTTPException(
            status_code=400, detail="Not a supported disc image format",
        )

    try:
        info = await dolphin_tool_service.header(path)
        return DolphinDiscInfo(
            file=path,
            game_id=info.get("game_id"),
            game_name=info.get("game_name") or info.get("name"),
            disc_number=info.get("disc_number") or info.get("disc"),
            revision=info.get("revision"),
            region=info.get("region"),
            format=info.get("format"),
            compression=info.get("compression"),
            block_size=info.get("block_size"),
            file_size=info.get("file_size"),
            raw_data=info.get("raw_data", ""),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read disc info: {e!s}",
        )


@router.get("/dolphin-verify")
async def verify_dolphin(
    path: str = Query(
        ..., description="Path to disc image to verify",
    ),
) -> dict:
    """Verify the integrity of a GameCube/Wii disc image."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_dolphin_file(path):
        raise HTTPException(
            status_code=400, detail="Not a supported disc image format",
        )

    verify_token = await _acquire_verify_lane_or_429()
    try:
        result = await dolphin_tool_service.verify(path)
        if result.get("valid"):
            await verification_store.mark_verified(path)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to verify disc image: {e!s}",
        )
    finally:
        verify_token.release()


@router.get("/dolphin-verify/events")
async def verify_dolphin_events(
    path: str = Query(
        ..., description="Path to disc image to verify",
    ),
) -> EventSourceResponse:
    """Stream disc image verification progress updates."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_dolphin_file(path):
        raise HTTPException(
            status_code=400, detail="Not a supported disc image format",
        )

    async def event_generator():
        verify_token = await workload_limiter.try_acquire("verify")
        if verify_token is None:
            yield {
                "event": "verify_error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "valid": False,
                        "message": _verification_backpressure_detail(),
                    },
                ),
            }
            return

        queue: asyncio.Queue = asyncio.Queue()
        done = asyncio.Event()
        start = time.monotonic()

        async def run_verify():
            try:
                async for update in dolphin_tool_service.verify_stream(
                    path,
                ):
                    await queue.put(update)
            except Exception as exc:
                await queue.put(
                    {"type": "error", "valid": False, "message": str(exc)},
                )
            finally:
                done.set()

        verify_task = asyncio.create_task(run_verify())
        try:
            while True:
                try:
                    update = await asyncio.wait_for(
                        queue.get(), timeout=2,
                    )
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - start)
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(
                            {
                                "progress": None,
                                "message": f"Verifying... ({elapsed}s)",
                            },
                        ),
                    }
                    if done.is_set() and queue.empty():
                        break
                    continue

                if update.get("type") == "progress":
                    yield {
                        "event": "verify_progress",
                        "data": json.dumps(update),
                    }
                elif update.get("type") == "complete":
                    if update.get("valid"):
                        await verification_store.mark_verified(path)
                    yield {
                        "event": "verify_complete",
                        "data": json.dumps(update),
                    }
                    break
                elif update.get("type") == "error":
                    yield {
                        "event": "verify_error",
                        "data": json.dumps(update),
                    }
                    break
        finally:
            verify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verify_task
            verify_token.release()

    return EventSourceResponse(event_generator())

@router.get("/z3ds-info", response_model=Z3DSInfo)
async def get_z3ds_info(
    path: str = Query(..., description="Path to 3DS ROM file"),
):
    """Get basic information about a Nintendo 3DS ROM file."""
    if not await run_in_threadpool(
        is_within_configured_volumes, path, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside configured volumes",
        )
    if not await run_in_threadpool(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="File not found")
    if not _is_z3ds_info_file(path):
        raise HTTPException(
            status_code=400,
            detail="Not a supported 3DS ROM format (.3ds, .cci, .cia, .z3ds, .zcci, .zcia)",
        )

    try:
        info = await run_in_threadpool(z3ds_compress_service.info, path)
        return Z3DSInfo(
            file=info["file"],
            size=info["size"],
            size_display=info["size_display"],
            format=info.get("format"),
            extension=info["extension"],
            compressed=info["compressed"],
            compression_type=info.get("compression_type"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read 3DS ROM info: {e!s}",
        )
