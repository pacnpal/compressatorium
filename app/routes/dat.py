"""API routes for MAME Redump DAT file management and hash matching."""

import asyncio
import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import settings
from services.dat_store import dat_store
from services.file_hasher import compute_file_sha1
from services.workload_limiter import workload_limiter
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = logging.getLogger("chd.dat")


class MatchRequest(BaseModel):
    path: str


class MatchBatchRequest(BaseModel):
    paths: list[str]


class SyncRequest(BaseModel):
    tag: str | None = None


@router.post("/dat/import")
async def import_dat(file: UploadFile = File(...)):
    """Import a MAME Redump DAT file (Logiqx XML format)."""
    if not file.filename or not file.filename.lower().endswith((".dat", ".xml")):
        raise HTTPException(
            status_code=400,
            detail="File must be a .dat or .xml file",
        )

    # Stream upload to a temp file to avoid holding the full content in memory
    max_size = 100 * 1024 * 1024  # 100MB
    total = 0
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise HTTPException(
                        status_code=400, detail="DAT file too large (max 100MB)"
                    )
                await run_in_threadpool(tmp.write, chunk)

        try:
            # Pass the temp file path (not its contents) so parse_dat() can
            # iterparse directly from disk without a second in-memory copy.
            result = await dat_store.import_dat(tmp_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return result


@router.get("/dat/list")
async def list_dats():
    """List all imported DATs."""
    return await run_in_threadpool(dat_store.list_dats)


@router.delete("/dat/{dat_id}")
async def delete_dat(dat_id: str):
    """Delete an imported DAT."""
    deleted = await dat_store.delete_dat(dat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="DAT not found")
    return {"deleted": True, "id": dat_id}


@router.get("/dat/stats")
async def get_dat_stats():
    """Get DAT store statistics."""
    return await run_in_threadpool(dat_store.get_stats)


@router.post("/dat/match")
async def match_file(request: MatchRequest):
    """Match a single file against imported DATs."""
    # Resolve symlinks in a thread pool to avoid blocking the async event loop.
    # os.path.realpath and is_within_configured_volumes both perform filesystem
    # I/O; running them in the thread pool also ensures resolution errors (e.g.
    # on network filesystems) surface as a 4xx rather than an unhandled 500.
    normalized_path = await run_in_threadpool(os.path.realpath, request.path)

    if not await run_in_threadpool(is_within_configured_volumes, normalized_path):
        raise HTTPException(status_code=403, detail="Access denied")

    if not await run_in_threadpool(os.path.isfile, normalized_path):
        raise HTTPException(status_code=404, detail="File not found")

    result = await _match_single_file(normalized_path)
    return result


def _resolve_and_group_paths(
    paths: list[str],
) -> tuple[dict[str, list[str]], set[str]]:
    """Resolve paths and group by resolved form; identify denied paths.

    Both os.path.realpath and is_within_configured_volumes perform filesystem
    I/O, so this helper is intended to be called inside run_in_threadpool.

    os.path.realpath handles symlink loops gracefully by detecting the loop
    (via ELOOP) and returning a best-effort absolute path rather than raising.
    is_within_configured_volumes internally uses pathlib.Path.resolve(), which
    raises RuntimeError for symlink loops; that exception is caught inside
    path_utils._resolve_path (which returns None), causing the volume check to
    return False and the path to be added to denied_normalized.

    Returns:
        normalized_to_originals: resolved_path → list of original input paths
            that resolve to it (so alias inputs share one cache lookup).
        denied_normalized: resolved paths that lie outside configured volumes.
    """
    normalized_to_originals: dict[str, list[str]] = {}
    for p in paths:
        normalized = os.path.realpath(p)
        normalized_to_originals.setdefault(normalized, []).append(p)
    denied_normalized = {
        norm
        for norm in normalized_to_originals
        if not is_within_configured_volumes(norm)
    }
    return normalized_to_originals, denied_normalized


@router.post("/dat/match-batch")
async def match_batch(request: MatchBatchRequest):
    """Match multiple files against imported DATs."""
    if not await run_in_threadpool(dat_store.has_dats):
        return {"results": {p: {"path": p, "matched": False} for p in request.paths}}

    # Resolve all paths and check volume membership in a single thread-pool
    # call to avoid blocking the async event loop with filesystem I/O.
    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths
    )

    # Check cached matches using normalized paths
    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )
    results: dict[str, dict] = {}
    to_compute: list[str] = []  # normalized paths

    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            result = {"path": normalized_path, "matched": False, "error": "access denied"}
            for original_path in original_paths:
                results[original_path] = result
            continue
        cached_result = cached.get(normalized_path)
        if cached_result is not None:
            for original_path in original_paths:
                results[original_path] = cached_result
        else:
            to_compute.append(normalized_path)

    # Compute matches for uncached files
    new_matches: dict[str, dict] = {}
    for normalized_path in to_compute:
        exists = await run_in_threadpool(os.path.isfile, normalized_path)
        if not exists:
            result = {"path": normalized_path, "matched": False}
            # Don't cache missing-file results: the file may appear later and
            # a stale negative entry would not be cleared by prune_missing.
        else:
            result = await _match_single_file(normalized_path)
            new_matches[normalized_path] = result
        for original_path in normalized_to_originals[normalized_path]:
            results[original_path] = result

    # Cache new results using normalized path keys
    if new_matches:
        await dat_store.set_matches_batch(new_matches)

    return {"results": results}


@router.post("/dat/prune")
async def prune_missing():
    """Remove match cache entries for files that no longer exist."""
    removed = await dat_store.prune_missing()
    return {"removed": removed}


# ---------------------------------------------------------------------------
# MAMERedump sync endpoints
# ---------------------------------------------------------------------------

def _get_sync_service():
    from services.dat_sync import dat_sync_service
    return dat_sync_service


@router.post("/dat/sync")
async def sync_mameredump(http_request: Request, request: SyncRequest | None = None):
    """Trigger a sync of all DAT files from the MAMERedump GitHub repo.

    The sync runs in the background. Poll ``/dat/sync/status`` for progress.
    """
    svc = _get_sync_service()
    if svc.is_syncing:
        raise HTTPException(status_code=409, detail="Sync already in progress")

    tag = request.tag if request else None

    async def _run_sync():
        await svc.sync(tag=tag)

    task = asyncio.create_task(_run_sync())

    # Keep a strong reference so the Task isn't garbage-collected before it
    # finishes; the done callback removes it from the shared app-level set.
    bg_tasks: set[asyncio.Task] = http_request.app.state.background_tasks
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

    def _log_bg_error(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "dat_sync background task failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    task.add_done_callback(_log_bg_error)

    # Yield to the event loop so the background task has an opportunity to enter
    # svc.sync() and claim the syncing lock before we respond.  svc.sync()
    # acquires _syncing under a threading.Lock (no awaits before that point),
    # so in practice a single yield is sufficient — but asyncio scheduling order
    # is not a documented guarantee, so this is a best-effort check rather than
    # a hard guarantee.  After asyncio.sleep(0), if the task is already done it
    # raised immediately (e.g. a near-simultaneous request already held the
    # lock) and we can return the correct status code.
    await asyncio.sleep(0)

    if task.done():
        try:
            task.result()
        except RuntimeError as exc:
            if "Sync already in progress" in str(exc):
                raise HTTPException(status_code=409, detail="Sync already in progress")
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync")
        except Exception:
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync")

    return {"status": "started", "message": "Sync started"}


@router.get("/dat/sync/status")
async def sync_status():
    """Return the current sync status."""
    return _get_sync_service().get_status()


@router.post("/dat/sync/cancel")
async def sync_cancel():
    """Cancel an in-progress sync."""
    if _get_sync_service().cancel():
        return {"status": "cancelling"}
    raise HTTPException(status_code=409, detail="No sync in progress")


async def _match_single_file(file_path: str) -> dict:
    """Match a file against all imported DATs.

    For CHD files: tries header SHA1 and data_SHA1 from metadata store.
    For all files: falls back to file-level SHA1.
    """
    ext = os.path.splitext(file_path)[1].lower()
    base_result = {"path": file_path, "matched": False}

    if not await run_in_threadpool(dat_store.has_dats):
        return base_result

    # For CHD files, try the header hashes first (fast, already cached)
    if ext == ".chd":
        match = await _try_chd_header_match(file_path)
        if match:
            return match

    # Defense-in-depth: respect the operator-configured size cap so
    # browsing a folder of 8 GB Wii ISOs doesn't stampede the hasher.
    size_cap = max(0, int(getattr(settings, "match_max_file_size", 0) or 0))
    if size_cap > 0:
        try:
            size_bytes = await run_in_threadpool(os.path.getsize, file_path)
        except OSError:
            size_bytes = 0
        if size_bytes > size_cap:
            return {
                **base_result,
                "reason": "file too large",
                "file_size": size_bytes,
            }

    # File-level SHA1 (works for any format). Gate under the "match"
    # workload lane so ``MAX_MATCH_CONCURRENCY`` bounds how many full-
    # file hashes run at once when a directory of uncached files is
    # browsed.
    try:
        async with await workload_limiter.acquire("match"):
            file_sha1 = await compute_file_sha1(file_path)
    except OSError as exc:
        logger.warning("Failed to hash %s: %s", file_path, exc)
        return base_result

    record = await run_in_threadpool(dat_store.lookup_sha1, file_sha1)
    if record:
        dat_name = await run_in_threadpool(dat_store.get_dat_name, record.get("dat_id", ""))
        return {
            "path": file_path,
            "matched": True,
            "dat_id": record.get("dat_id"),
            "dat_name": dat_name,
            "game_name": record.get("game_name"),
            "rom_name": record.get("rom_name"),
            "match_type": "file_sha1",
            "file_hash": file_sha1,
        }

    return base_result


async def _try_chd_header_match(file_path: str) -> dict | None:
    """Try matching CHD file using header SHA1 and data_SHA1 from metadata store."""
    from services.chd_metadata_store import chd_metadata_store
    metadata = await chd_metadata_store.get_metadata(file_path)
    if not metadata:
        return None

    # Try overall SHA1 from CHD header
    sha1 = metadata.get("sha1", "").strip().lower()
    if sha1:
        record = await run_in_threadpool(dat_store.lookup_sha1, sha1)
        if record:
            dat_name = await run_in_threadpool(dat_store.get_dat_name, record.get("dat_id", ""))
            return {
                "path": file_path,
                "matched": True,
                "dat_id": record.get("dat_id"),
                "dat_name": dat_name,
                "game_name": record.get("game_name"),
                "rom_name": record.get("rom_name"),
                "match_type": "chd_sha1",
                "file_hash": sha1,
            }

    # Try data SHA1 (uncompressed content hash)
    data_sha1 = metadata.get("data_sha1", "").strip().lower()
    if data_sha1:
        record = await run_in_threadpool(dat_store.lookup_sha1, data_sha1)
        if record:
            dat_name = await run_in_threadpool(dat_store.get_dat_name, record.get("dat_id", ""))
            return {
                "path": file_path,
                "matched": True,
                "dat_id": record.get("dat_id"),
                "dat_name": dat_name,
                "game_name": record.get("game_name"),
                "rom_name": record.get("rom_name"),
                "match_type": "chd_data_sha1",
                "file_hash": data_sha1,
            }

    return None
