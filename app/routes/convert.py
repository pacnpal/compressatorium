import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from models import (
    ConversionJob,
    JobCreateRequest,
    BatchJobCreateRequest,
    JobStatus,
    DuplicateAction,
    CheckDuplicatesRequest,
    DuplicateInfo,
)
from services.job_manager import job_manager
from services.archive import archive_service
from services.chdman import chdman_service
from services.lock_manager import lock_manager
from utils.path_utils import is_within_configured_volumes

router = APIRouter()


def normalize_compression(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    invalid = [p for p in parts if not re.fullmatch(r"[a-z0-9]+", p)]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid compression token(s): {', '.join(invalid)}",
        )
    return ",".join(parts)


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


def check_output_conflicts(mode: str, output_path: str) -> tuple[bool, bool]:
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


@router.post("/jobs/check-duplicates", response_model=List[DuplicateInfo])
async def check_duplicates(request: CheckDuplicatesRequest):
    """Check which output files already exist for the given input files."""
    results = []
    mode = request.mode.value

    if request.output_dir and not is_within_configured_volumes(
        request.output_dir, treat_archives=False
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
        effective_output_dir = request.output_dir

        if "::" in file_path:
            # For archive files, use the internal filename for the CHD name
            # and save next to the archive (unless output_dir is specified)
            archive_path, internal_path = file_path.split("::", 1)
            actual_filename = internal_path
            if not effective_output_dir:
                effective_output_dir = os.path.dirname(archive_path)

        if "::" in file_path:
            output_stem = archive_service._output_stem_for_member(actual_filename)
            output_path = chdman_service.get_output_path_for_mode(
                mode, output_stem, effective_output_dir, treat_as_stem=True
            )
        else:
            output_path = chdman_service.get_output_path_for_mode(
                mode, actual_filename, effective_output_dir
            )
        exists, _ = check_output_conflicts(mode, output_path)

        results.append(
            DuplicateInfo(file_path=file_path, output_path=output_path, exists=exists)
        )

    return results


@router.post("/jobs", response_model=ConversionJob)
async def create_job(request: JobCreateRequest):
    """Create a single conversion job."""
    compression = normalize_compression(request.compression)
    mode = request.mode.value
    if compression and mode.startswith("extract"):
        raise HTTPException(
            status_code=400,
            detail="Compression is only supported for CHD creation/copy",
        )
    if not is_within_configured_volumes(request.file_path):
        raise HTTPException(
            status_code=403,
            detail="Access denied: file path outside configured volumes",
        )

    if request.output_dir and not is_within_configured_volumes(
        request.output_dir, treat_archives=False
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
        if mode.startswith("extract") or mode == "copy":
            raise HTTPException(
                status_code=400,
                detail="Archive inputs are not supported for extract/copy",
            )
        archive_path, internal_path = request.file_path.split("::", 1)
        archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive
        display_filename = os.path.basename(internal_path)

        if not os.path.isfile(archive_path):
            raise HTTPException(status_code=404, detail="Archive not found")

        # Calculate output path before extraction to avoid unnecessary work
        effective_output_dir = request.output_dir or archive_source_dir
        output_stem = archive_service._output_stem_for_member(internal_path)
        output_path = chdman_service.get_output_path_for_mode(
            mode, output_stem, effective_output_dir, treat_as_stem=True
        )

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(
                    status_code=409, detail="Output file already exists"
                )
            elif request.duplicate_action == DuplicateAction.OVERWRITE:
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
        mode.startswith("extract") or mode == "copy"
    ) and not file_path.lower().endswith(".chd"):
        raise HTTPException(
            status_code=400, detail="Extract/copy modes require .chd input files"
        )

    if mode.startswith("create") and file_path.lower().endswith(".chd"):
        raise HTTPException(
            status_code=400, detail="Create modes require non-CHD input files"
        )

    # Calculate output path and handle duplicates
    # For archive files: use output_dir if specified, otherwise save next to archive
    if output_path is None:
        effective_output_dir = request.output_dir or archive_source_dir
        output_path = chdman_service.get_output_path_for_mode(
            mode, file_path, effective_output_dir
        )

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(
                    status_code=409, detail="Output file already exists"
                )
            elif request.duplicate_action == DuplicateAction.OVERWRITE:
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

    allow_overwrite = (
        request.duplicate_action == DuplicateAction.OVERWRITE and output_exists
    )
    job = job_manager.create_job(
        file_path,
        request.mode,
        output_path=output_path,
        allow_overwrite=allow_overwrite,
        filename_override=display_filename,
        compression=compression,
    )

    return job


@router.post("/jobs/batch", response_model=List[ConversionJob])
async def create_batch_jobs(request: BatchJobCreateRequest):
    """Create multiple conversion jobs."""
    compression = normalize_compression(request.compression)
    mode = request.mode.value
    if compression and mode.startswith("extract"):
        raise HTTPException(
            status_code=400,
            detail="Compression is only supported for CHD creation/copy",
        )
    for file_path in request.file_paths:
        if not is_within_configured_volumes(file_path):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: {file_path} outside configured volumes",
            )

    if request.output_dir and not is_within_configured_volumes(
        request.output_dir, treat_archives=False
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: output directory outside configured volumes",
        )

    jobs = []
    skipped = []

    for file_path in request.file_paths:
        archive_source_dir = None  # Directory where the archive is located (for output)
        output_path = None
        output_exists = False
        display_filename = None

        # Handle archive files
        if "::" in file_path:
            if mode.startswith("extract") or mode == "copy":
                skipped.append(file_path)
                continue
            archive_path, internal_path = file_path.split("::", 1)
            archive_source_dir = os.path.dirname(
                archive_path
            )  # Save CHD next to archive
            display_filename = os.path.basename(internal_path)

            if not os.path.isfile(archive_path):
                skipped.append(file_path)
                continue

            # Calculate output path before extraction to avoid unnecessary work
            effective_output_dir = request.output_dir or archive_source_dir
            output_stem = archive_service._output_stem_for_member(internal_path)
            output_path = chdman_service.get_output_path_for_mode(
                mode, output_stem, effective_output_dir, treat_as_stem=True
            )
            output_exists, is_locked = check_output_conflicts(mode, output_path)

            if output_exists or is_locked:
                if request.duplicate_action == DuplicateAction.SKIP:
                    skipped.append(file_path)
                    continue
                elif request.duplicate_action == DuplicateAction.OVERWRITE:
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
            mode.startswith("extract") or mode == "copy"
        ) and not file_path.lower().endswith(".chd"):
            skipped.append(file_path)
            continue

        if mode.startswith("create") and file_path.lower().endswith(".chd"):
            skipped.append(file_path)
            continue

        # Calculate output path and handle duplicates
        # For archive files: use output_dir if specified, otherwise save next to archive
        if output_path is None:
            effective_output_dir = request.output_dir or archive_source_dir
            output_path = chdman_service.get_output_path_for_mode(
                mode, file_path, effective_output_dir
            )
            output_exists, is_locked = check_output_conflicts(mode, output_path)

            if output_exists or is_locked:
                if request.duplicate_action == DuplicateAction.SKIP:
                    skipped.append(file_path)
                    continue
                elif request.duplicate_action == DuplicateAction.OVERWRITE:
                    if is_locked:
                        skipped.append(file_path)
                        continue
                elif request.duplicate_action == DuplicateAction.RENAME:
                    if mode == "extractcd":
                        output_path = get_unique_output_path_for_extractcd(output_path)
                    else:
                        output_path = get_unique_output_path(output_path)

        allow_overwrite = (
            request.duplicate_action == DuplicateAction.OVERWRITE and output_exists
        )
        job = job_manager.create_job(
            file_path,
            request.mode,
            output_path=output_path,
            allow_overwrite=allow_overwrite,
            filename_override=display_filename,
            compression=compression,
        )
        jobs.append(job)

    return jobs


@router.get("/jobs", response_model=List[ConversionJob])
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

                except Exception as e:
                    # Log error but keep the connection alive
                    print(f"SSE event generator error: {e}")
                    await asyncio.sleep(1)
        finally:
            for job_id, queue in list(queues.items()):
                job_manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())


@router.get("/jobs/{job_id}", response_model=ConversionJob)
async def get_job(job_id: str):
    """Get a specific job by ID."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/jobs/completed")
async def delete_completed_jobs():
    """Delete all completed, failed, and cancelled jobs."""
    deleted_ids = []
    for job in list(job_manager.get_all_jobs()):
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            if job_manager.delete_job(job.id):
                deleted_ids.append(job.id)
    return {"deleted": deleted_ids, "count": len(deleted_ids)}


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a job."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        # Just remove from list
        job_manager.delete_job(job_id)
        return {"status": "deleted"}

    if job_manager.cancel_job(job_id):
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
                    }
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
                        }
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
