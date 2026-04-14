"""API routes for MAME Redump DAT file management and hash matching."""

import asyncio
import logging
import os
import stat
import tempfile
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import settings
from models import ConversionMode
from services.dat_store import dat_store
from services.file_hasher import compute_file_sha1
from services.job_manager import job_manager
from services.workload_limiter import workload_limiter
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = logging.getLogger("chd.dat")


# Guards concurrent bulk match jobs. Only one DAT-match background job runs
# at a time; concurrent requests return 409 (matches the /dat/sync pattern).
#
# Scope of this guard: **single-process** only.  The container entrypoint
# pins uvicorn to ``--workers 1`` (see entrypoint.sh), so module-level
# state is the authoritative source of truth across all requests hitting
# this app.  If the deployment ever moves to multi-worker / multi-pod,
# replace this with a distributed lock (Redis, SQLite advisory lock, or
# a dedicated matches-scheduler process) — every worker would otherwise
# get its own independent lock and the 409 guard would no longer hold.
#
# Lazy-initialised: on Python 3.10+ ``asyncio.Lock()`` no longer binds a loop
# at construction (the deprecated ``loop=`` kwarg is gone), but its internal
# waiter state still ties to whichever loop first touches it.  pytest-asyncio
# creates a fresh event loop per test, so a module-level Lock constructed at
# import time can end up wedged to a stale loop across test runs.  Binding
# on first ``async with`` keeps the Lock local to the *current* loop and
# sidesteps any cold-import-order surprises.
_match_job_lock: asyncio.Lock | None = None
_active_match_job_id: str | None = None


def _get_match_job_lock() -> asyncio.Lock:
    """Return the module-level match-job lock, creating it on first use.

    Must be called from a running event loop so the Lock binds to it.
    """
    global _match_job_lock  # noqa: PLW0603 — intentional module-level state
    if _match_job_lock is None:
        _match_job_lock = asyncio.Lock()
    return _match_job_lock


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
            # Don't cache size-cap skips: the result is configuration-dependent.
            # If MATCH_MAX_FILE_SIZE is later raised or disabled the file must
            # be re-hashed rather than being served a stale "too large" entry.
            # Don't cache hash errors either: a transient OSError must not be
            # persisted as a permanent negative entry.
            if not result.get("reason") and not result.get("error"):
                new_matches[normalized_path] = result
        for original_path in normalized_to_originals[normalized_path]:
            results[original_path] = result

    # Cache new results using normalized path keys
    if new_matches:
        await dat_store.set_matches_batch(new_matches)

    return {"results": results}


class MatchCacheLookupRequest(BaseModel):
    paths: list[str]


@router.post("/dat/matches/lookup")
async def match_cache_lookup(request: MatchCacheLookupRequest):
    """Read-only cache lookup for DAT matches.

    Returns whatever is already cached in ``dat_matches`` for the given
    paths. Does **not** hash uncached files. Used by the frontend to
    progressively populate match badges while a background
    ``/dat/match-batch/job`` is running.
    """
    if not request.paths:
        return {"results": {}}

    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths,
    )
    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )
    results: dict[str, dict] = {}
    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            for original_path in original_paths:
                results[original_path] = {
                    "path": original_path,
                    "matched": False,
                    "error": "access denied",
                }
            continue
        cached_entry = cached.get(normalized_path)
        if cached_entry is None:
            continue
        for original_path in original_paths:
            results[original_path] = cached_entry
    return {"results": results}


@router.post("/dat/match-batch/job")
async def match_batch_job(request: MatchBatchRequest, background_tasks: BackgroundTasks):
    """Start a background DAT-match job for a batch of files.

    Mirrors the metadata-scan UX: registers an external job in the Jobs
    panel, hashes uncached files serially under the ``match`` workload
    lane, persists cacheable results to the ``dat_matches`` cache
    incrementally as each file completes, and emits progress via
    :func:`job_manager.update_external_job`. The frontend polls
    ``/dat/matches/lookup`` as progress ticks arrive so badges can flip
    progressively from "DAT …" to a concrete cached result rather than
    all-at-once at the end.

    Note:
      ``/dat/matches/lookup`` is a read-only view of persisted
      ``dat_matches`` entries. Not every processed path is guaranteed to
      become available there: non-cacheable outcomes (for example missing
      files, ``reason == "file too large"``, or transient hash/stat
      errors) are intentionally not persisted in the cache, so those
      paths may never appear in ``/dat/matches/lookup``.
    Returns:
      * ``{"status": "idle", "results": <cached>}`` when every
        requested path is already cached (fast path, no job created).
      * ``{"status": "started", "job_id": "..."}`` when at least one
        path needs hashing.
      * HTTP 409 when another match job is already active.
    """
    global _active_match_job_id

    if not request.paths:
        return {"status": "idle", "results": {}}

    if not await run_in_threadpool(dat_store.has_dats):
        return {
            "status": "idle",
            "results": {p: {"path": p, "matched": False} for p in request.paths},
        }

    normalized_to_originals, denied_normalized = await run_in_threadpool(
        _resolve_and_group_paths, request.paths,
    )

    cached = await run_in_threadpool(
        dat_store.get_matches_batch, list(normalized_to_originals.keys()),
    )

    results: dict[str, dict] = {}
    to_compute: list[str] = []
    for normalized_path, original_paths in normalized_to_originals.items():
        if normalized_path in denied_normalized:
            for original_path in original_paths:
                results[original_path] = {
                    "path": original_path,
                    "matched": False,
                    "error": "access denied",
                }
            continue
        cached_entry = cached.get(normalized_path)
        if cached_entry is not None:
            for original_path in original_paths:
                results[original_path] = cached_entry
            continue
        # Existence check happens inside the job loop so a missing file
        # doesn't fail the whole request — it just gets a matched=false
        # entry without being cached (same behaviour as sync /match-batch).
        to_compute.append(normalized_path)

    if not to_compute:
        return {"status": "idle", "results": results}

    async with _get_match_job_lock():
        # Authoritative guard: if the module-level id is set, a background
        # _run_match_job is still executing (it only clears the id from
        # its own finally block AFTER the hashing loop has fully unwound
        # and finish_external_job has landed).  Do NOT fall back to
        # inspecting the job_manager's visible status here — the job can
        # be reaped from job_manager.jobs via "Clear Done" or a history
        # prune while the underlying task is still alive, which would
        # spuriously let a second job start and race the first on the
        # "match" workload lane.  The presence of _active_match_job_id
        # is the single source of truth for "a hash loop is still
        # executing"; anything else is advisory.
        if _active_match_job_id is not None:
            raise HTTPException(
                status_code=409,
                detail="DAT match job already in progress",
            )

        scan_job = job_manager.create_external_job(
            filename="DAT Match",
            mode=ConversionMode.DAT_MATCH,
            message=f"Hashing {len(to_compute)} file(s)\u2026",
        )
        _active_match_job_id = scan_job.id

    background_tasks.add_task(
        _run_match_job,
        job_id=scan_job.id,
        paths_to_compute=to_compute,
    )

    return {
        "status": "started",
        "job_id": scan_job.id,
        "results": results,
    }


async def _hash_one_for_job(normalized_path: str) -> tuple[dict, bool]:
    """Compute a match result for one path inside the background job loop.

    Returns ``(result, cacheable)``.  ``cacheable`` is ``False`` for
    transient failures (missing file, stat error, hasher exception) and
    configuration-dependent skips (size-cap hit); those must not be
    persisted to ``dat_matches`` because they would stick around after
    the underlying condition changes (file appears, cap is raised,
    transient OSError clears).
    """
    try:
        st = await run_in_threadpool(os.stat, normalized_path)
    except FileNotFoundError:
        return {"path": normalized_path, "matched": False}, False
    except OSError as exc:
        logger.warning("Failed to stat %s: %s", normalized_path, exc)
        return {"path": normalized_path, "matched": False, "error": str(exc)}, False

    if not stat.S_ISREG(st.st_mode):
        return {"path": normalized_path, "matched": False}, False

    try:
        result = await _match_single_file(normalized_path)
    except Exception as exc:  # pragma: no cover — isolated per-path
        logger.warning("DAT match failed for %s: %s", normalized_path, exc)
        return {"path": normalized_path, "matched": False, "error": str(exc)}, False

    if result.get("reason") == "file too large" or result.get("error"):
        return result, False
    return result, True


async def _run_match_job(
    *,
    job_id: str,
    paths_to_compute: list[str],
) -> None:
    """Background task: hash paths serially, cache results, tick progress."""
    global _active_match_job_id

    start = time.monotonic()
    total = len(paths_to_compute)
    processed = 0
    hashed = 0
    matched = 0
    job_success = False
    job_error: str | None = None

    try:
        for idx, normalized_path in enumerate(paths_to_compute, start=1):
            display_name = os.path.basename(normalized_path) or normalized_path
            await job_manager.update_external_job(
                job_id,
                progress=int(100 * (idx - 1) / total) if total else 0,
                message=f"[{idx}/{total}] {display_name}",
            )

            result, cacheable = await _hash_one_for_job(normalized_path)
            if cacheable:
                hashed += 1
                # Per-file writes are intentional: persist each completed
                # result immediately with the single-row API so the cache
                # remains durable if the job is cancelled or the process
                # crashes mid-run, without paying the extra preload/prefetch
                # work of the batch upsert path for a one-item write.
                try:
                    await dat_store.set_match(normalized_path, result)
                except Exception as exc:  # pragma: no cover — best-effort cache write
                    logger.warning("Failed to cache match for %s: %s", normalized_path, exc)

            processed += 1
            if result.get("matched"):
                matched += 1

        job_success = True
    except Exception as exc:
        logger.exception("DAT match job %s failed", job_id)
        job_error = str(exc)
    finally:
        elapsed = time.monotonic() - start
        if job_success:
            final_msg = (
                f"{processed}/{total} processed, {hashed} hashed, {matched} matched"
                f" \u2014 {elapsed:.1f}s"
            )
        else:
            final_msg = f"DAT match failed: {job_error or 'unknown error'}"
        await job_manager.update_external_job(
            job_id,
            progress=100 if job_success else None,
            message=final_msg,
        )
        await job_manager.finish_external_job(
            job_id,
            success=job_success,
            error_message=job_error,
        )
        async with _get_match_job_lock():
            if _active_match_job_id == job_id:
                _active_match_job_id = None


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
                raise HTTPException(
                    status_code=409, detail="Sync already in progress"
                ) from exc
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync") from exc
        except Exception as exc:
            logger.exception("dat_sync background task failed at startup")
            raise HTTPException(status_code=500, detail="Failed to start sync") from exc

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
        logger.warning("Failed to hash %s", file_path, exc_info=True)
        return {**base_result, "error": "Unable to process file"}

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
