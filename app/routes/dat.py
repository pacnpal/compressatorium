"""API routes for MAME Redump DAT file management and hash matching."""

import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from services.dat_store import dat_store
from services.file_hasher import compute_file_sha1
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = logging.getLogger("chd.dat")


class MatchRequest(BaseModel):
    path: str


class MatchBatchRequest(BaseModel):
    paths: list[str]


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

        def _read_tmp():
            with open(tmp_path, encoding="utf-8") as fh:
                return fh.read()

        try:
            xml_content = await run_in_threadpool(_read_tmp)
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    try:
        result = await dat_store.import_dat(xml_content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return result


@router.get("/dat/list")
async def list_dats():
    """List all imported DATs."""
    return dat_store.list_dats()


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
    return dat_store.get_stats()


@router.post("/dat/match")
async def match_file(request: MatchRequest):
    """Match a single file against imported DATs."""
    # Normalize the requested path to avoid traversal or malformed paths
    normalized_path = os.path.normpath(os.path.abspath(request.path))

    if not is_within_configured_volumes(normalized_path):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isfile(normalized_path):
        raise HTTPException(status_code=404, detail="File not found")

    result = await _match_single_file(normalized_path)
    return result


@router.post("/dat/match-batch")
async def match_batch(request: MatchBatchRequest):
    """Match multiple files against imported DATs."""
    if not dat_store.has_dats():
        return {"results": {p: {"path": p, "matched": False} for p in request.paths}}

    # Normalize all input paths upfront for consistent volume checks,
    # file existence checks, hashing, cache keys, and result path fields.
    path_map = {
        p: os.path.normpath(os.path.abspath(p)) for p in request.paths
    }

    # Check cached matches using normalized paths
    cached = dat_store.get_matches_batch(list(path_map.values()))
    results: dict[str, dict] = {}
    to_compute: list[str] = []  # normalized paths

    for original_path, normalized_path in path_map.items():
        if not is_within_configured_volumes(normalized_path):
            results[original_path] = {"path": normalized_path, "matched": False, "error": "access denied"}
            continue
        if cached.get(normalized_path) is not None:
            results[original_path] = cached[normalized_path]
        else:
            to_compute.append(normalized_path)

    # Compute matches for uncached files
    new_matches: dict[str, dict] = {}
    # Build a reverse map from normalized path → original path for O(1) lookup
    normalized_to_original = {n: o for o, n in path_map.items()}
    for normalized_path in to_compute:
        original_path = normalized_to_original[normalized_path]
        exists = await run_in_threadpool(os.path.isfile, normalized_path)
        if not exists:
            result = {"path": normalized_path, "matched": False}
        else:
            result = await _match_single_file(normalized_path)
        results[original_path] = result
        new_matches[normalized_path] = result

    # Cache new results using normalized path keys
    if new_matches:
        await dat_store.set_matches_batch(new_matches)

    return {"results": results}


@router.post("/dat/prune")
async def prune_missing():
    """Remove match cache entries for files that no longer exist."""
    removed = await dat_store.prune_missing()
    return {"removed": removed}


async def _match_single_file(file_path: str) -> dict:
    """Match a file against all imported DATs.

    For CHD files: tries header SHA1 and data_SHA1 from metadata store.
    For all files: falls back to file-level SHA1.
    """
    ext = os.path.splitext(file_path)[1].lower()
    base_result = {"path": file_path, "matched": False}

    if not dat_store.has_dats():
        return base_result

    # For CHD files, try the header hashes first (fast, already cached)
    if ext == ".chd":
        match = await _try_chd_header_match(file_path)
        if match:
            return match

    # File-level SHA1 (works for any format)
    try:
        file_sha1 = await compute_file_sha1(file_path)
    except OSError as exc:
        logger.warning("Failed to hash %s: %s", file_path, exc)
        return base_result

    record = dat_store.lookup_sha1(file_sha1)
    if record:
        return {
            "path": file_path,
            "matched": True,
            "dat_id": record.get("dat_id"),
            "dat_name": dat_store.get_dat_name(record.get("dat_id", "")),
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
        record = dat_store.lookup_sha1(sha1)
        if record:
            return {
                "path": file_path,
                "matched": True,
                "dat_id": record.get("dat_id"),
                "dat_name": dat_store.get_dat_name(record.get("dat_id", "")),
                "game_name": record.get("game_name"),
                "rom_name": record.get("rom_name"),
                "match_type": "chd_sha1",
                "file_hash": sha1,
            }

    # Try data SHA1 (uncompressed content hash)
    data_sha1 = metadata.get("data_sha1", "").strip().lower()
    if data_sha1:
        record = dat_store.lookup_sha1(data_sha1)
        if record:
            return {
                "path": file_path,
                "matched": True,
                "dat_id": record.get("dat_id"),
                "dat_name": dat_store.get_dat_name(record.get("dat_id", "")),
                "game_name": record.get("game_name"),
                "rom_name": record.get("rom_name"),
                "match_type": "chd_data_sha1",
                "file_hash": data_sha1,
            }

    return None
