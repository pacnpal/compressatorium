"""API routes for MAME Redump DAT file management and hash matching."""

import logging
import os

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

    # Read with size limit to prevent memory exhaustion
    max_size = 100 * 1024 * 1024  # 100MB
    chunks = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status_code=400, detail="DAT file too large (max 100MB)")
        chunks.append(chunk)
    content = b"".join(chunks)

    try:
        xml_content = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")

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
    if not is_within_configured_volumes(request.path):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isfile(request.path):
        raise HTTPException(status_code=404, detail="File not found")

    result = await _match_single_file(request.path)
    return result


@router.post("/dat/match-batch")
async def match_batch(request: MatchBatchRequest):
    """Match multiple files against imported DATs."""
    if not dat_store.has_dats():
        return {"results": {p: {"path": p, "matched": False} for p in request.paths}}

    # Check cached matches first
    cached = dat_store.get_matches_batch(request.paths)
    results = {}
    to_compute = []

    for path in request.paths:
        if not is_within_configured_volumes(path):
            results[path] = {"path": path, "matched": False, "error": "access denied"}
            continue
        if cached.get(path) is not None:
            results[path] = cached[path]
        else:
            to_compute.append(path)

    # Compute matches for uncached files
    new_matches = {}
    for path in to_compute:
        exists = await run_in_threadpool(os.path.isfile, path)
        if not exists:
            result = {"path": path, "matched": False}
        else:
            result = await _match_single_file(path)
        results[path] = result
        new_matches[path] = result

    # Cache new results
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
        dat_info = _get_dat_name(record.get("dat_id", ""))
        return {
            "path": file_path,
            "matched": True,
            "dat_id": record.get("dat_id"),
            "dat_name": dat_info,
            "game_name": record.get("game_name"),
            "rom_name": record.get("rom_name"),
            "match_type": "file_sha1",
            "file_hash": file_sha1,
        }

    return base_result


async def _try_chd_header_match(file_path: str) -> dict | None:
    """Try matching CHD file using header SHA1 and data_SHA1 from metadata store."""
    try:
        from services.chd_metadata_store import chd_metadata_store
        metadata = chd_metadata_store.get(file_path)
        if not metadata:
            return None

        # Try overall SHA1 from CHD header
        sha1 = metadata.get("sha1", "").strip().lower()
        if sha1:
            record = dat_store.lookup_sha1(sha1)
            if record:
                dat_info = _get_dat_name(record.get("dat_id", ""))
                return {
                    "path": file_path,
                    "matched": True,
                    "dat_id": record.get("dat_id"),
                    "dat_name": dat_info,
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
                dat_info = _get_dat_name(record.get("dat_id", ""))
                return {
                    "path": file_path,
                    "matched": True,
                    "dat_id": record.get("dat_id"),
                    "dat_name": dat_info,
                    "game_name": record.get("game_name"),
                    "rom_name": record.get("rom_name"),
                    "match_type": "chd_data_sha1",
                    "file_hash": data_sha1,
                }

    except Exception as exc:
        logger.debug("CHD header match failed for %s: %s", file_path, exc)

    return None


def _get_dat_name(dat_id: str) -> str:
    """Get DAT name from ID."""
    dats = dat_store.list_dats()
    for dat in dats:
        if dat.get("id") == dat_id:
            return dat.get("name", "Unknown")
    return "Unknown"
