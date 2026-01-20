import asyncio
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.models import (
    ConversionJob, JobCreateRequest, BatchJobCreateRequest,
    ConversionMode, JobStatus
)
from app.services.job_manager import job_manager
from app.services.archive import archive_service

router = APIRouter()


def validate_path(path: str) -> bool:
    """Validate that a path is within configured volumes."""
    # Handle archive paths (archive_path::internal_path)
    if "::" in path:
        path = path.split("::")[0]

    real_path = os.path.realpath(path)
    for volume in settings.volumes:
        real_volume = os.path.realpath(volume)
        if real_path.startswith(real_volume + os.sep) or real_path == real_volume:
            return True
    return False


def validate_output_dir(output_dir: Optional[str]) -> bool:
    """Validate output directory is within configured volumes."""
    if output_dir is None:
        return True
    return validate_path(output_dir)


@router.post("/jobs", response_model=ConversionJob)
async def create_job(request: JobCreateRequest):
    """Create a single conversion job."""
    if not validate_path(request.file_path):
        raise HTTPException(status_code=403, detail="Access denied: file path outside configured volumes")

    if not validate_output_dir(request.output_dir):
        raise HTTPException(status_code=403, detail="Access denied: output directory outside configured volumes")

    # Handle archive files
    file_path = request.file_path
    temp_dir = None

    if "::" in request.file_path:
        # Extract from archive
        archive_path, internal_path = request.file_path.split("::", 1)
        file_path, temp_dir = archive_service.extract_file(archive_path, internal_path)
        # Note: temp_dir cleanup should be handled after conversion

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    job = job_manager.create_job(file_path, request.mode, request.output_dir)

    # Store temp_dir reference for cleanup (simplified - in production use proper cleanup)
    if temp_dir:
        job.message = f"temp:{temp_dir}"

    return job


@router.post("/jobs/batch", response_model=List[ConversionJob])
async def create_batch_jobs(request: BatchJobCreateRequest):
    """Create multiple conversion jobs."""
    for file_path in request.file_paths:
        if not validate_path(file_path):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: {file_path} outside configured volumes"
            )

    if not validate_output_dir(request.output_dir):
        raise HTTPException(status_code=403, detail="Access denied: output directory outside configured volumes")

    jobs = []
    for file_path in request.file_paths:
        actual_path = file_path
        temp_dir = None

        # Handle archive files
        if "::" in file_path:
            archive_path, internal_path = file_path.split("::", 1)
            actual_path, temp_dir = archive_service.extract_file(archive_path, internal_path)

        if os.path.isfile(actual_path):
            job = job_manager.create_job(actual_path, request.mode, request.output_dir)
            if temp_dir:
                job.message = f"temp:{temp_dir}"
            jobs.append(job)

    return jobs


@router.get("/jobs", response_model=List[ConversionJob])
async def list_jobs():
    """List all conversion jobs."""
    return job_manager.get_all_jobs()


@router.get("/jobs/{job_id}", response_model=ConversionJob)
async def get_job(job_id: str):
    """Get a specific job by ID."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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


@router.get("/jobs/events")
async def job_events():
    """SSE endpoint for all job progress updates."""
    async def event_generator():
        # Create a queue to receive all job updates
        queues = {}

        while True:
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
                        "data": update
                    }

                    # Unsubscribe if job is done
                    if update.get("type") in ("complete", "error"):
                        job_manager.unsubscribe(job_id, queue)
                        del queues[job_id]

                except asyncio.QueueEmpty:
                    pass

            await asyncio.sleep(0.1)

    return EventSourceResponse(event_generator())


@router.get("/jobs/{job_id}/events")
async def job_progress(job_id: str):
    """SSE endpoint for a specific job's progress."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    queue = job_manager.subscribe(job_id)

    async def event_generator():
        try:
            # Send initial state
            yield {
                "event": "status",
                "data": {
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": job.progress
                }
            }

            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {
                        "event": update.get("type", "progress"),
                        "data": update
                    }

                    if update.get("type") in ("complete", "error"):
                        break

                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {"event": "ping", "data": {}}

        finally:
            job_manager.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())
