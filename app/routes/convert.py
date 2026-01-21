import asyncio
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from models import (
    ConversionJob, JobCreateRequest, BatchJobCreateRequest,
    JobStatus, DuplicateAction, CheckDuplicatesRequest, DuplicateInfo
)
from services.job_manager import job_manager
from services.archive import archive_service
from services.chdman import chdman_service
from services.lock_manager import lock_manager
from services.verification_store import verification_store
from utils.path_utils import is_within_configured_volumes

router = APIRouter()


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


@router.post("/jobs/check-duplicates", response_model=List[DuplicateInfo])
async def check_duplicates(request: CheckDuplicatesRequest):
    """Check which output files already exist for the given input files."""
    results = []

    if request.output_dir and not is_within_configured_volumes(request.output_dir, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: output directory outside configured volumes")

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
            output_path = chdman_service.get_chd_path(output_stem, effective_output_dir, treat_as_stem=True)
        else:
            output_path = chdman_service.get_chd_path(actual_filename, effective_output_dir)
        file_exists, is_locked = lock_manager.check_file_status(output_path)
        exists = file_exists or is_locked

        results.append(DuplicateInfo(
            file_path=file_path,
            output_path=output_path,
            exists=exists
        ))

    return results


@router.post("/jobs", response_model=ConversionJob)
async def create_job(request: JobCreateRequest):
    """Create a single conversion job."""
    if not is_within_configured_volumes(request.file_path):
        raise HTTPException(status_code=403, detail="Access denied: file path outside configured volumes")

    if request.output_dir and not is_within_configured_volumes(request.output_dir, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: output directory outside configured volumes")

    # Handle archive files
    file_path = request.file_path
    temp_dir = None
    archive_source_dir = None  # Directory where the archive is located (for output)
    output_path = None

    if "::" in request.file_path:
        archive_path, internal_path = request.file_path.split("::", 1)
        archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive

        # Calculate output path before extraction to avoid unnecessary work
        effective_output_dir = request.output_dir or archive_source_dir
        output_stem = archive_service._output_stem_for_member(internal_path)
        output_path = chdman_service.get_chd_path(output_stem, effective_output_dir, treat_as_stem=True)

        file_exists, is_locked = lock_manager.check_file_status(output_path)
        if file_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(status_code=409, detail="Output file already exists")
            elif request.duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise HTTPException(status_code=409, detail="Output file is currently being converted")
                if file_exists:
                    os.remove(output_path)
                    verification_store.clear(output_path)
            elif request.duplicate_action == DuplicateAction.RENAME:
                output_path = get_unique_output_path(output_path)

        # Extract from archive only if we're going to convert
        try:
            file_path, temp_dir = archive_service.extract_file(archive_path, internal_path)
            archive_service.extract_related_files(archive_path, internal_path, temp_dir)
        except (ValueError, FileNotFoundError) as exc:
            if temp_dir:
                archive_service.cleanup_temp_dir(temp_dir)
            raise HTTPException(status_code=400, detail=f"Failed to extract from archive: {exc}")
        # Note: temp_dir cleanup should be handled after conversion

    if not os.path.isfile(file_path):
        if temp_dir:
            archive_service.cleanup_temp_dir(temp_dir)
        raise HTTPException(status_code=404, detail="File not found")

    # Calculate output path and handle duplicates
    # For archive files: use output_dir if specified, otherwise save next to archive
    if output_path is None:
        effective_output_dir = request.output_dir or archive_source_dir
        output_path = chdman_service.get_chd_path(file_path, effective_output_dir)

        file_exists, is_locked = lock_manager.check_file_status(output_path)
        if file_exists or is_locked:
            if request.duplicate_action == DuplicateAction.SKIP:
                raise HTTPException(status_code=409, detail="Output file already exists")
            elif request.duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise HTTPException(status_code=409, detail="Output file is currently being converted")
                if file_exists:
                    os.remove(output_path)
                    verification_store.clear(output_path)
            elif request.duplicate_action == DuplicateAction.RENAME:
                output_path = get_unique_output_path(output_path)

    job = job_manager.create_job(file_path, request.mode, output_path=output_path)

    # Store temp_dir reference for cleanup (simplified - in production use proper cleanup)
    if temp_dir:
        job.temp_dir = temp_dir

    return job


@router.post("/jobs/batch", response_model=List[ConversionJob])
async def create_batch_jobs(request: BatchJobCreateRequest):
    """Create multiple conversion jobs."""
    for file_path in request.file_paths:
        if not is_within_configured_volumes(file_path):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: {file_path} outside configured volumes"
            )

    if request.output_dir and not is_within_configured_volumes(request.output_dir, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: output directory outside configured volumes")

    jobs = []
    skipped = []

    for file_path in request.file_paths:
        actual_path = file_path
        temp_dir = None
        archive_source_dir = None  # Directory where the archive is located (for output)
        output_path = None

        # Handle archive files
        if "::" in file_path:
            archive_path, internal_path = file_path.split("::", 1)
            archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive

            # Calculate output path before extraction to avoid unnecessary work
            effective_output_dir = request.output_dir or archive_source_dir
            output_stem = archive_service._output_stem_for_member(internal_path)
            output_path = chdman_service.get_chd_path(output_stem, effective_output_dir, treat_as_stem=True)
            file_exists, is_locked = lock_manager.check_file_status(output_path)

            if file_exists or is_locked:
                if request.duplicate_action == DuplicateAction.SKIP:
                    skipped.append(file_path)
                    continue
                elif request.duplicate_action == DuplicateAction.OVERWRITE:
                    if is_locked:
                        skipped.append(file_path)
                        continue
                    if file_exists:
                        os.remove(output_path)
                        verification_store.clear(output_path)
                elif request.duplicate_action == DuplicateAction.RENAME:
                    output_path = get_unique_output_path(output_path)

            try:
                actual_path, temp_dir = archive_service.extract_file(archive_path, internal_path)
                archive_service.extract_related_files(archive_path, internal_path, temp_dir)
            except (ValueError, FileNotFoundError):
                if temp_dir:
                    archive_service.cleanup_temp_dir(temp_dir)
                skipped.append(file_path)
                continue

        if os.path.isfile(actual_path):
            # Calculate output path and handle duplicates
            # For archive files: use output_dir if specified, otherwise save next to archive
            if output_path is None:
                effective_output_dir = request.output_dir or archive_source_dir
                output_path = chdman_service.get_chd_path(actual_path, effective_output_dir)
                file_exists, is_locked = lock_manager.check_file_status(output_path)

                if file_exists or is_locked:
                    if request.duplicate_action == DuplicateAction.SKIP:
                        skipped.append(file_path)
                        continue
                    elif request.duplicate_action == DuplicateAction.OVERWRITE:
                        if is_locked:
                            skipped.append(file_path)
                            continue
                        if file_exists:
                            os.remove(output_path)
                            verification_store.clear(output_path)
                    elif request.duplicate_action == DuplicateAction.RENAME:
                        output_path = get_unique_output_path(output_path)

            job = job_manager.create_job(actual_path, request.mode, output_path=output_path)
            if temp_dir:
                job.temp_dir = temp_dir
            jobs.append(job)
        elif temp_dir:
            archive_service.cleanup_temp_dir(temp_dir)

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
                        if job.id not in queues and job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
                            queues[job.id] = job_manager.subscribe(job.id)

                    # Check all queues for updates
                    for job_id, queue in list(queues.items()):
                        try:
                            update = queue.get_nowait()
                            yield {
                                "event": update.get("type", "progress"),
                                "data": json.dumps(update)
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
                "data": json.dumps({
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": job.progress
                })
            }

            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                return

            queue = job_manager.subscribe(job_id)
            latest_job = job_manager.get_job(job_id)
            if latest_job and latest_job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                yield {
                    "event": "status",
                    "data": json.dumps({
                        "job_id": job_id,
                        "status": latest_job.status.value,
                        "progress": latest_job.progress
                    })
                }
                return

            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": update.get("type", "progress"),
                        "data": json.dumps(update)
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
