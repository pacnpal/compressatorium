import asyncio
import logging
import os
import re
from pathlib import Path

from config import settings
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from models import (
    BatchJobCreateRequest,
    CheckDuplicatesRequest,
    ConversionJob,
    ConversionMode,
    DeletePlanRequest,
    DuplicateAction,
    DuplicateInfo,
    JobCreateRequest,
    JobStatus,
)
from services.archive import archive_service
from services.chdman import chdman_service
from services.dolphin_tool import dolphin_tool_service
from services.z3ds_compress import z3ds_compress_service
from services.job_manager import QueueBackpressureError, job_manager
from services.lock_manager import lock_manager
from services.tools import ModeKind, registry
from sse_starlette.sse import EventSourceResponse
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = logging.getLogger("chd")


def normalize_output_dir(value: str | None) -> str | None:
    """Normalize and validate the output directory string.

    Parameters
    ----------
    value : Optional[str]
        The output directory path as a string, or None.

    Returns
    -------
    Optional[str]
        The cleaned output directory string, or None if not provided.

    Raises
    ------
    HTTPException
        If the output directory is an empty string after stripping.

    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Output directory cannot be empty")
    return cleaned


def normalize_compression(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    invalid = [
        p for p in parts
        if not re.fullmatch(r"[a-z0-9]+(?::[0-9]+)?", p)
    ]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid compression token(s): {', '.join(invalid)}",
        )
    return ",".join(parts)





def supports_delete_on_verify(mode: str) -> bool:
    try:
        return registry.spec(mode).supports_delete_on_verify
    except KeyError:
        return False


def _is_dolphin_mode(mode: str) -> bool:
    return mode.startswith("dolphin_")


def _get_output_path(mode, input_path, output_dir, *, treat_as_stem=False):
    if _is_dolphin_mode(mode):
        return dolphin_tool_service.get_output_path_for_mode(
            mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )
    if mode == ConversionMode.Z3DS_COMPRESS.value:
        return z3ds_compress_service.get_output_path_for_mode(
            mode, input_path, output_dir, treat_as_stem=treat_as_stem,
        )
    return chdman_service.get_output_path_for_mode(
        mode, input_path, output_dir, treat_as_stem=treat_as_stem,
    )


def _is_same_path(path_a: str, path_b: str) -> bool:
    try:
        return os.path.realpath(path_a) == os.path.realpath(path_b)
    except OSError:
        return False


def get_disallowed_archive_paths(file_paths: list[str]) -> set[str]:
    """Get archive paths that should not allow delete-on-verify due to multiple selections."""
    archive_counts = {}
    for file_path in file_paths:
        if "::" not in file_path:
            continue
        archive_path = file_path.split("::", 1)[0]
        archive_counts[archive_path] = archive_counts.get(archive_path, 0) + 1
    return {
        archive_path
        for archive_path, count in archive_counts.items()
        if count > 1
    }


def get_unique_output_path(base_path: str) -> str:
    """Generate a unique output path by appending a number if the file exists."""
    file_exists, is_locked = lock_manager.check_file_status(base_path)
    if not file_exists and not is_locked:
        return base_path

    path = Path(base_path)
    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        file_exists, is_locked = lock_manager.check_file_status(str(new_path))
        if not file_exists and not is_locked:
            return str(new_path)
        counter += 1


def check_output_conflicts(mode: str, output_path: str) -> tuple:
    file_exists, is_locked = lock_manager.check_file_status(output_path)
    exists = file_exists or is_locked
    locked = is_locked

    if mode == "extractcd":
        bin_path = str(Path(output_path).with_suffix(".bin"))
        bin_exists, bin_locked = lock_manager.check_file_status(bin_path)
        exists = exists or bin_exists or bin_locked
        locked = locked or bin_locked

    return exists, locked


def get_unique_output_path_for_extractcd(base_path: str) -> str:
    path = Path(base_path)
    stem = path.stem
    suffix = path.suffix or ".cue"
    parent = path.parent

    counter = 1
    candidate = str(path)
    while True:
        exists, locked = check_output_conflicts("extractcd", candidate)
        if not exists and not locked:
            return candidate
        candidate = str(parent / f"{stem}_{counter}{suffix}")
        counter += 1


@router.post("/jobs/check-duplicates", response_model=list[DuplicateInfo])
async def check_duplicates(request: CheckDuplicatesRequest):
    """Check which output files already exist for the given input files."""
    if request.mode in (ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH):
        raise HTTPException(
            status_code=400,
            detail=f"{request.mode.value} is not a valid conversion mode",
        )
    results = []
    mode = request.mode.value
    output_dir = normalize_output_dir(request.output_dir)

    if output_dir and not is_within_configured_volumes(
        output_dir, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: output directory outside configured volumes",
        )

    for file_path in request.file_paths:
        if not is_within_configured_volumes(file_path):
            continue

        # Handle archive paths - get the actual filename and determine output location
        actual_filename = file_path
        effective_output_dir = output_dir

        if "::" in file_path:
            # For archive files, use the internal filename for the CHD name
            # and save next to the archive (unless output_dir is specified)
            archive_path, internal_path = file_path.split("::", 1)
            actual_filename = internal_path
            if not effective_output_dir:
                effective_output_dir = os.path.dirname(archive_path)

        if "::" in file_path:
            output_stem = archive_service._output_stem_for_member(actual_filename)
            output_path = _get_output_path(
                mode, output_stem, effective_output_dir, treat_as_stem=True,
            )
        else:
            output_path = _get_output_path(
                mode, actual_filename, effective_output_dir,
            )
        exists, _ = check_output_conflicts(mode, output_path)

        results.append(
            DuplicateInfo(file_path=file_path, output_path=output_path, exists=exists),
        )

    return results


@router.post("/jobs/delete-plan")
async def delete_plan(request: DeletePlanRequest) -> dict:
    """Build a delete plan for delete-on-verify confirmation."""
    if not request.file_paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    mode = request.mode.value
    if not supports_delete_on_verify(mode):
        raise HTTPException(
            status_code=400,
            detail="Delete-on-verify is only supported for create/copy/Dolphin/3DS modes",
        )

    disallowed_archives = get_disallowed_archive_paths(request.file_paths)

    items = []
    blocked = False
    total_delete_count = 0

    for file_path in request.file_paths:
        item = None
        if not is_within_configured_volumes(file_path):
            item = {
                "source_path": file_path,
                "delete_paths": [],
                "missing_paths": [],
                "unsafe_paths": ["Source path outside configured volumes"],
                "errors": [],
            }
        else:
            item = await run_in_threadpool(build_delete_plan, file_path)

        if "::" in file_path:
            archive_path = file_path.split("::", 1)[0]
            if archive_path in disallowed_archives:
                item.setdefault("errors", []).append(
                    "Delete-on-verify is not supported for multiple selections"
                    " from the same archive",
                )

        items.append(item)
        total_delete_count += len(item.get("delete_paths", []))
        if item.get("errors") or item.get("unsafe_paths") or item.get("missing_paths"):
            blocked = True

    return {
        "items": items,
        "blocked": blocked,
        "total_delete_count": total_delete_count,
    }


@router.post("/jobs", response_model=ConversionJob)
async def create_job(request: JobCreateRequest):
    """Create a single conversion job."""
    if request.mode in (ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH):
        raise HTTPException(
            status_code=400,
            detail=f"{request.mode.value} is not a valid conversion mode",
        )
    compression = normalize_compression(request.compression)
    mode = request.mode.value
    output_dir = normalize_output_dir(request.output_dir)
    spec = registry.spec(mode)
    is_dolphin = spec.tool_id == "dolphin"
    is_z3ds = spec.tool_id == "z3ds"
    if compression and spec.tool_id == "chdman" and spec.kind == ModeKind.EXTRACT:
        raise HTTPException(
            status_code=400,
            detail="Compression is only supported for CHD creation/copy",
        )
    if compression and not is_dolphin and ":" in compression:
        raise HTTPException(
            status_code=400,
            detail="Compression levels are only supported for Dolphin formats",
        )
    if compression and mode == "dolphin_iso":
        raise HTTPException(
            status_code=400,
            detail="Compression not applicable for ISO extraction",
        )
    if compression and mode == "dolphin_gcz":
        raise HTTPException(
            status_code=400,
            detail="GCZ uses fixed internal compression",
        )
    if compression and is_dolphin and "," in compression:
        raise HTTPException(
            status_code=400,
            detail="Dolphin compression supports only one codec at a time",
        )
    if request.delete_on_verify and not spec.supports_delete_on_verify:
        raise HTTPException(
            status_code=400,
            detail="Delete-on-verify is only supported for create/copy/Dolphin/3DS modes",
        )
    if not is_within_configured_volumes(request.file_path):
        raise HTTPException(
            status_code=403,
            detail="Access denied: file path outside configured volumes",
        )

    if output_dir and not is_within_configured_volumes(
        output_dir, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: output directory outside configured volumes",
        )

    # Handle archive files
    file_path = request.file_path
    archive_source_dir = None  # Directory where the archive is located (for output)
    output_path = None
    output_exists = False
    display_filename = None

    if "::" in request.file_path:
        if not spec.allows_archive_input:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Archive inputs are not supported for"
                    " extract/copy/dolphin/z3ds_compress modes"
                ),
            )
        archive_path, internal_path = request.file_path.split("::", 1)
        archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive
        display_filename = os.path.basename(internal_path)

        if not os.path.isfile(archive_path):
            raise HTTPException(status_code=404, detail="Archive not found")

        # Calculate output path before extraction to avoid unnecessary work
        effective_output_dir = output_dir or archive_source_dir
        output_stem = archive_service._output_stem_for_member(internal_path)
        output_path = _get_output_path(
            mode, output_stem, effective_output_dir, treat_as_stem=True,
        )

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(
                    status_code=409, detail="Output file already exists",
                )
            if request.duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise HTTPException(
                        status_code=409,
                        detail="Output file is currently being converted",
                    )
            elif request.duplicate_action == DuplicateAction.RENAME:
                if mode == "extractcd":
                    output_path = get_unique_output_path_for_extractcd(output_path)
                else:
                    output_path = get_unique_output_path(output_path)

    if "::" not in request.file_path and not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if (
        spec.tool_id == "chdman" and spec.kind in (ModeKind.EXTRACT, ModeKind.COPY)
    ) and not file_path.lower().endswith(".chd"):
        raise HTTPException(
            status_code=400, detail="Extract/copy modes require .chd input files",
        )

    if spec.kind == ModeKind.CREATE and file_path.lower().endswith(".chd"):
        raise HTTPException(
            status_code=400, detail="Create modes require non-CHD input files",
        )

    if is_dolphin:
        ext = Path(file_path).suffix.lower()
        if ext not in spec.input_extensions:
            raise HTTPException(
                status_code=400,
                detail="Dolphin modes require GameCube/Wii disc images "
                       "(.iso, .gcz, .wia, .rvz, .wbfs)",
            )

    if is_z3ds:
        ext = Path(file_path).suffix.lower()
        if ext not in spec.input_extensions:
            raise HTTPException(
                status_code=400,
                detail="z3ds_compress mode requires Nintendo 3DS ROM files (.cci, .cia, .3ds)",
            )

    # Calculate output path and handle duplicates
    # For archive files: use output_dir if specified, otherwise save next to archive
    if output_path is None:
        effective_output_dir = output_dir or archive_source_dir
        output_path = _get_output_path(
            mode, file_path, effective_output_dir,
        )

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(
                    status_code=409, detail="Output file already exists",
                )
            if request.duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise HTTPException(
                        status_code=409,
                        detail="Output file is currently being converted",
                    )
            elif request.duplicate_action == DuplicateAction.RENAME:
                if mode == "extractcd":
                    output_path = get_unique_output_path_for_extractcd(output_path)
                else:
                    output_path = get_unique_output_path(output_path)

    if (
        is_dolphin
        and request.duplicate_action == DuplicateAction.OVERWRITE
        and output_path
        and _is_same_path(output_path, file_path)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Output path matches input; overwriting would delete the source file"
            ),
        )

    allow_overwrite = (
        request.duplicate_action == DuplicateAction.OVERWRITE and output_exists
    )
    delete_snapshot = None
    if request.delete_on_verify:
        try:
            delete_snapshot = await run_in_threadpool(
                build_delete_snapshot, file_path,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Delete-on-verify blocked: {exc}",
            ) from None

    # Proactive queue-depth check: reject with 429 before spending work
    # on job construction if the queue is already at capacity.  Parity
    # with the batch-create path, and surfaces backpressure even when
    # tests or callers stub out ``job_manager.create_job``.
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    if 0 < max_depth <= job_manager.get_queue_depth():
        raise HTTPException(
            status_code=429,
            detail=f"Conversion queue full ({max_depth} jobs). Retry later.",
        )

    try:
        job = await job_manager.create_job(
            file_path,
            request.mode,
            output_path=output_path,
            allow_overwrite=allow_overwrite,
            filename_override=display_filename,
            compression=compression,
            delete_on_verify=request.delete_on_verify,
            delete_snapshot=delete_snapshot,
        )
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc

    return job


@router.post("/jobs/batch", response_model=list[ConversionJob])
async def create_batch_jobs(request: BatchJobCreateRequest):
    """Create multiple conversion jobs."""
    if request.mode in (ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH):
        raise HTTPException(
            status_code=400,
            detail=f"{request.mode.value} is not a valid conversion mode",
        )
    compression = normalize_compression(request.compression)
    mode = request.mode.value
    spec = registry.spec(mode)
    is_dolphin = spec.tool_id == "dolphin"
    is_z3ds = spec.tool_id == "z3ds"
    output_dir = normalize_output_dir(request.output_dir)
    if compression and spec.tool_id == "chdman" and spec.kind == ModeKind.EXTRACT:
        raise HTTPException(
            status_code=400,
            detail="Compression is only supported for CHD creation/copy",
        )
    if compression and not is_dolphin and ":" in compression:
        raise HTTPException(
            status_code=400,
            detail="Compression levels are only supported for Dolphin formats",
        )
    if compression and mode == "dolphin_iso":
        raise HTTPException(
            status_code=400,
            detail="Compression not applicable for ISO extraction",
        )
    if compression and mode == "dolphin_gcz":
        raise HTTPException(
            status_code=400,
            detail="GCZ uses fixed internal compression",
        )
    if compression and is_dolphin and "," in compression:
        raise HTTPException(
            status_code=400,
            detail="Dolphin compression supports only one codec at a time",
        )
    if request.delete_on_verify and not spec.supports_delete_on_verify:
        raise HTTPException(
            status_code=400,
            detail="Delete-on-verify is only supported for create/copy/Dolphin/3DS modes",
        )
    if request.delete_on_verify:
        disallowed_archives = get_disallowed_archive_paths(request.file_paths)
        if disallowed_archives:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Delete-on-verify is not supported for multiple selections from the "
                    "same archive"
                ),
            )
    for file_path in request.file_paths:
        if not is_within_configured_volumes(file_path):
            raise HTTPException(
                status_code=403,
                detail="Access denied: path outside configured volumes",
            )

    if output_dir and not is_within_configured_volumes(
        output_dir, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: output directory outside configured volumes",
        )

    def _input_extension(path: str) -> str:
        if "::" in path:
            _, internal = path.split("::", 1)
            return Path(internal).suffix.lower()
        return Path(path).suffix.lower()

    def _priority(ext: str) -> int:
        if ext in {".cue", ".gdi"}:
            return 4
        if ext == ".iso":
            return 3
        if ext == ".bin":
            return 1
        return 0

    skipped = []
    candidates = []

    for file_path in request.file_paths:
        archive_source_dir = None  # Directory where the archive is located (for output)
        output_path = None
        base_output_path = None
        output_exists = False
        display_filename = None

        # Handle archive files
        if "::" in file_path:
            if not spec.allows_archive_input:
                skipped.append(file_path)
                continue
            archive_path, internal_path = file_path.split("::", 1)
            archive_source_dir = os.path.dirname(
                archive_path,
            )  # Save CHD next to archive
            display_filename = os.path.basename(internal_path)

            if not os.path.isfile(archive_path):
                skipped.append(file_path)
                continue

            # Calculate output path before extraction to avoid unnecessary work
            effective_output_dir = output_dir or archive_source_dir
            output_stem = archive_service._output_stem_for_member(internal_path)
            output_path = _get_output_path(
                mode, output_stem, effective_output_dir, treat_as_stem=True,
            )
            base_output_path = output_path
            output_exists, is_locked = check_output_conflicts(mode, output_path)

            if output_exists or is_locked:
                if request.duplicate_action == DuplicateAction.SKIP:
                    skipped.append(file_path)
                    continue
                if request.duplicate_action == DuplicateAction.OVERWRITE:
                    if is_locked:
                        skipped.append(file_path)
                        continue
                elif request.duplicate_action == DuplicateAction.RENAME:
                    if mode == "extractcd":
                        output_path = get_unique_output_path_for_extractcd(output_path)
                    else:
                        output_path = get_unique_output_path(output_path)

        if "::" not in file_path and not os.path.isfile(file_path):
            skipped.append(file_path)
            continue

        if (
            spec.tool_id == "chdman" and spec.kind in (ModeKind.EXTRACT, ModeKind.COPY)
        ) and not file_path.lower().endswith(".chd"):
            skipped.append(file_path)
            continue

        if spec.kind == ModeKind.CREATE and file_path.lower().endswith(".chd"):
            skipped.append(file_path)
            continue

        if is_dolphin:
            ext = Path(file_path).suffix.lower()
            if ext not in spec.input_extensions:
                skipped.append(file_path)
                continue

        if is_z3ds:
            ext = Path(file_path).suffix.lower()
            if ext not in spec.input_extensions:
                skipped.append(file_path)
                continue

        # Calculate output path and handle duplicates
        # For archive files: use output_dir if specified, otherwise save next to archive
        if output_path is None:
            effective_output_dir = output_dir or archive_source_dir
            output_path = _get_output_path(
                mode, file_path, effective_output_dir,
            )
            base_output_path = output_path
            output_exists, is_locked = check_output_conflicts(mode, output_path)

            if output_exists or is_locked:
                if request.duplicate_action == DuplicateAction.SKIP:
                    skipped.append(file_path)
                    continue
                if request.duplicate_action == DuplicateAction.OVERWRITE:
                    if is_locked:
                        skipped.append(file_path)
                        continue
                elif request.duplicate_action == DuplicateAction.RENAME:
                    if mode == "extractcd":
                        output_path = get_unique_output_path_for_extractcd(output_path)
                    else:
                        output_path = get_unique_output_path(output_path)

        if (
            is_dolphin
            and request.duplicate_action == DuplicateAction.OVERWRITE
            and output_path
            and _is_same_path(output_path, file_path)
        ):
            skipped.append(file_path)
            continue

        allow_overwrite = (
            request.duplicate_action == DuplicateAction.OVERWRITE and output_exists
        )
        delete_snapshot = None
        if request.delete_on_verify:
            try:
                delete_snapshot = await run_in_threadpool(
                    build_delete_snapshot, file_path,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Delete-on-verify blocked for {file_path}: {exc}",
                ) from None

        candidates.append(
            {
                "file_path": file_path,
                "output_path": output_path,
                "base_output_path": base_output_path or output_path,
                "allow_overwrite": allow_overwrite,
                "display_filename": display_filename,
                "priority": _priority(_input_extension(file_path)),
                "delete_snapshot": delete_snapshot,
            },
        )

    selected = {}
    order = []
    for candidate in candidates:
        key = candidate["base_output_path"]
        existing = selected.get(key)
        if not existing:
            selected[key] = candidate
            order.append(key)
            continue
        if candidate["priority"] > existing["priority"]:
            selected[key] = candidate

    if order:
        # Job creation will enforce backpressure atomically under lock.
        # No need for a pre-check here as it would race with the locked check.
        pass

    job_specs = []
    for key in order:
        candidate = selected[key]
        job_specs.append(
            {
                "file_path": candidate["file_path"],
                "output_path": candidate["output_path"],
                "allow_overwrite": candidate["allow_overwrite"],
                "filename_override": candidate["display_filename"],
                "delete_snapshot": candidate.get("delete_snapshot"),
            },
        )

    # Proactive batch backpressure: reject before enqueuing any jobs if
    # accepting the batch would push the queue past ``max_queue_depth``.
    # Keeps single-job and batch submission behaviour consistent.
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    if max_depth > 0:
        projected = job_manager.get_queue_depth() + len(job_specs)
        if projected > max_depth:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Conversion queue would exceed capacity "
                    f"({max_depth} jobs). Retry later."
                ),
            )

    try:
        jobs = await job_manager.create_jobs_atomic(
            job_specs,
            request.mode,
            compression=compression,
            delete_on_verify=request.delete_on_verify,
        )
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc

    return jobs


@router.get("/jobs", response_model=list[ConversionJob])
async def list_jobs():
    """List all conversion jobs."""
    return job_manager.get_all_jobs()


# NOTE: SSE endpoints must be defined BEFORE parameterized routes to avoid conflicts
@router.get("/jobs/events")
async def job_events():
    """SSE endpoint for all job progress updates."""
    import json

    async def event_generator():
        # Create a queue to receive all job updates
        queues = {}

        try:
            while True:
                try:
                    # Subscribe to any new jobs
                    for job in job_manager.get_all_jobs():
                        if job.id not in queues and job.status in (
                            JobStatus.QUEUED,
                            JobStatus.PROCESSING,
                        ):
                            queues[job.id] = job_manager.subscribe(job.id)

                    # Check all queues for updates
                    for job_id, queue in list(queues.items()):
                        try:
                            update = queue.get_nowait()
                            job = job_manager.get_job(job_id)
                            if job is not None:
                                update = {
                                    **update,
                                    "job": job.model_dump(mode="json"),
                                }
                            yield {
                                "event": update.get("type", "progress"),
                                "data": json.dumps(update),
                            }

                            # Unsubscribe if job is done
                            if update.get("type") in ("complete", "error", "cancelled"):
                                job_manager.unsubscribe(job_id, queue)
                                del queues[job_id]

                        except asyncio.QueueEmpty:
                            pass

                    await asyncio.sleep(0.1)

                except Exception:
                    # Log error but keep the connection alive
                    await asyncio.sleep(1)
        finally:
            for job_id, queue in list(queues.items()):
                job_manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())


@router.get("/jobs/stuck-status")
async def check_stuck_status():
    """Check if the job queue is in a stuck state."""
    return job_manager.get_stuck_state_info()


@router.get("/jobs/{job_id}", response_model=ConversionJob)
async def get_job(job_id: str):
    """Get a specific job by ID (including recently archived jobs)."""
    job = job_manager.get_job_for_lookup(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/jobs/completed")
async def delete_completed_jobs(request: Request):
    """Delete all completed, failed, and cancelled jobs."""
    confirmation = request.headers.get("x-chd-action-confirm", "")
    if confirmation != "clear-completed-jobs":
        raise HTTPException(
            status_code=400,
            detail="Missing confirmation header for clear-completed action",
        )

    deleted_ids = []
    for job in list(job_manager.get_all_jobs()):
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            if await job_manager.delete_job(job.id):
                deleted_ids.append(job.id)
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Clear completed requested from %s; deleted=%d",
        client_host,
        len(deleted_ids),
    )
    return {"deleted": deleted_ids, "count": len(deleted_ids)}


@router.post("/jobs/cancel-all")
async def cancel_all_jobs(request: Request):
    """Cancel all queued and processing jobs."""
    confirmation = request.headers.get("x-chd-action-confirm", "")
    if confirmation != "cancel-all-jobs":
        raise HTTPException(
            status_code=400,
            detail="Missing confirmation header for cancel-all action",
        )

    result = await job_manager.cancel_all_jobs()
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Cancel all requested from %s; queued=%d processing=%d requested=%d",
        client_host,
        result.get("queued", 0),
        result.get("processing", 0),
        result.get("requested", 0),
    )
    return result





@router.post("/jobs/recover")
async def recover_stuck_jobs():
    """Manually trigger recovery from a stuck job queue state.

    This endpoint can be called when jobs are queued but not processing,
    typically due to stale or orphaned locks.

    It attempts to clean up stale locks and restore the queue to a healthy state
    so that new or pending jobs can be processed again. It does not automatically
    restart or requeue individual jobs that were previously stuck.
    """
    result = await job_manager.recover_from_stuck_state()

    if not result.get("success"):
        raise HTTPException(
            status_code=429,
            detail=result.get("message", "Recovery failed")
        )

    return result


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        # Just remove from list
        await job_manager.delete_job(job_id)
        return {"status": "deleted"}

    if await job_manager.cancel_job(job_id):
        return {"status": "cancelled"}

    raise HTTPException(status_code=400, detail="Cannot cancel job")


@router.get("/jobs/{job_id}/events")
async def job_progress(job_id: str):
    """SSE endpoint for a specific job's progress."""
    import json

    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        queue = None
        try:
            # Send initial state
            yield {
                "event": "status",
                "data": json.dumps(
                    {
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                    },
                ),
            }

            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return

            queue = job_manager.subscribe(job_id)
            latest_job = job_manager.get_job(job_id)
            if latest_job and latest_job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                yield {
                    "event": "status",
                    "data": json.dumps(
                        {
                            "job_id": job_id,
                            "status": latest_job.status.value,
                            "progress": latest_job.progress,
                        },
                    ),
                }
                return

            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": update.get("type", "progress"),
                        "data": json.dumps(update),
                    }

                    if update.get("type") in ("complete", "error", "cancelled"):
                        break

                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {"event": "ping", "data": json.dumps({})}

        except Exception as e:
            print(f"SSE job progress error: {e}")
        finally:
            if queue:
                job_manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())
