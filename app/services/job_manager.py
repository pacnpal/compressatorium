import asyncio
import uuid
import os
import shutil
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Set

from app.models import ConversionJob, JobStatus, ConversionMode
from app.services.chdman import chdman_service, ConversionCancelled
from app.services.concurrency_manager import concurrency_manager
from app.services.lock_manager import lock_manager
from app.config import settings


class JobManager:
    """Manages conversion job queue and execution."""

    def __init__(self, max_concurrent: int = 2):
        self.jobs: OrderedDict[str, ConversionJob] = OrderedDict()
        self.max_concurrent = max(1, max_concurrent)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._cancelled: Set[str] = set()
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._running = False
        self._worker_tasks: List[asyncio.Task] = []

    def create_job(
        self,
        file_path: str,
        mode: ConversionMode,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None
    ) -> ConversionJob:
        """Create a new conversion job."""
        job_id = str(uuid.uuid4())[:8]
        filename = os.path.basename(file_path)

        # Determine output path - use explicit path if provided, otherwise calculate
        if output_path is None:
            output_path = chdman_service.get_chd_path(file_path, output_dir)

        job = ConversionJob(
            id=job_id,
            file_path=file_path,
            filename=filename,
            mode=mode,
            status=JobStatus.QUEUED,
            progress=0,
            created_at=datetime.utcnow(),
            output_path=output_path
        )

        self.jobs[job_id] = job
        self._queue.put_nowait(job_id)
        return job

    def create_batch_jobs(
        self,
        file_paths: List[str],
        mode: ConversionMode,
        output_dir: Optional[str] = None
    ) -> List[ConversionJob]:
        """Create multiple conversion jobs."""
        return [self.create_job(fp, mode, output_dir) for fp in file_paths]

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def get_all_jobs(self) -> List[ConversionJob]:
        """Get all jobs."""
        return list(self.jobs.values())

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job."""
        job = self.jobs.get(job_id)
        if not job:
            return False

        if job.status == JobStatus.QUEUED:
            self._cancelled.add(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.utcnow()
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            asyncio.create_task(self._notify_subscribers(job_id, {
                "type": "cancelled",
                "job_id": job_id,
                "status": job.status.value
            }))
            self._cleanup_temp_dir(job)
            return True

        if job.status == JobStatus.PROCESSING:
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            job.message = "Cancelling..."
            asyncio.create_task(self._notify_subscribers(job_id, {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message
            }))
            return True
        return False

    def delete_job(self, job_id: str) -> bool:
        """Delete a job from the list."""
        if job_id in self.jobs:
            job = self.jobs[job_id]
            if job.status == JobStatus.PROCESSING:
                self.cancel_job(job_id)
            self._cancelled.discard(job_id)
            if job_id not in self._cancel_events:
                self._cleanup_temp_dir(job)
            del self.jobs[job_id]
            return True
        return False

    def subscribe(self, job_id: str) -> asyncio.Queue:
        """Subscribe to progress updates for a job."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        if job_id not in self._subscribers:
            self._subscribers[job_id] = []
        self._subscribers[job_id].append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue):
        """Unsubscribe from progress updates."""
        if job_id in self._subscribers:
            try:
                self._subscribers[job_id].remove(queue)
            except ValueError:
                pass

    def _cleanup_temp_dir(self, job: ConversionJob):
        if job.temp_dir:
            temp_dir = job.temp_dir
            try:
                if temp_dir and os.path.isdir(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as cleanup_error:
                print(f"Failed to cleanup temp dir {temp_dir}: {cleanup_error}")
            finally:
                job.temp_dir = None

    async def _notify_subscribers(self, job_id: str, data: dict):
        """Notify all subscribers of a job update."""
        if job_id in self._subscribers:
            for queue in self._subscribers[job_id]:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    async def process_queue(self):
        """Background task to process conversion queue."""
        if self._running:
            return
        self._running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(worker_id))
            for worker_id in range(self.max_concurrent)
        ]
        await asyncio.gather(*self._worker_tasks)

    async def _worker_loop(self, worker_id: int):
        """Worker loop that processes jobs sequentially."""
        while self._running:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._process_job(job_id)
            except Exception as e:
                print(f"Worker {worker_id} error: {e}")
            finally:
                self._queue.task_done()

    async def _process_job(self, job_id: str):
        """Process a single conversion job."""
        job = self.jobs.get(job_id)
        if not job:
            return

        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            self._cancelled.discard(job_id)
            self._cleanup_temp_dir(job)
            return

        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            cancel_event.set()
            self._cancelled.discard(job_id)
            if not job.completed_at:
                job.completed_at = datetime.utcnow()
            await self._notify_subscribers(job_id, {
                "type": "cancelled",
                "job_id": job_id,
                "status": job.status.value
            })
            self._cleanup_temp_dir(job)
            del self._cancel_events[job_id]
            return

        slot_acquired = await concurrency_manager.acquire(job_id, cancel_event=cancel_event)
        if not slot_acquired:
            if job.status != JobStatus.CANCELLED:
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.utcnow()
                await self._notify_subscribers(job_id, {
                    "type": "cancelled",
                    "job_id": job_id,
                    "status": job.status.value
                })
            self._cleanup_temp_dir(job)
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            return

        # Try to acquire lock for the output file (prevents race conditions)
        lock_acquired = lock_manager.acquire_lock(job.output_path)
        if not lock_acquired:
            # Could not acquire lock - either file exists or is being converted
            # Check current status to provide better error message
            file_exists, is_locked = lock_manager.check_file_status(job.output_path)
            job.status = JobStatus.FAILED
            if is_locked:
                job.error_message = "Another job is already converting to this output file"
            elif file_exists:
                job.error_message = "Output CHD file already exists"
            else:
                job.error_message = "Could not acquire lock for output file"
            job.completed_at = datetime.utcnow()

            await self._notify_subscribers(job_id, {
                "type": "error",
                "job_id": job_id,
                "error": job.error_message
            })
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            concurrency_manager.release(job_id)
            self._cleanup_temp_dir(job)
            return

        job.status = JobStatus.PROCESSING
        job.started_at = datetime.utcnow()

        await self._notify_subscribers(job_id, {
            "type": "status",
            "job_id": job_id,
            "status": job.status.value,
            "progress": 0
        })

        try:
            async for update in chdman_service.convert(
                job.file_path,
                job.output_path,
                job.mode.value,
                cancel_event=cancel_event
            ):
                if cancel_event.is_set():
                    continue
                job.progress = update["progress"]
                job.message = update["message"]

                await self._notify_subscribers(job_id, {
                    "type": "progress",
                    "job_id": job_id,
                    "progress": job.progress,
                    "message": job.message
                })

            if job.status != JobStatus.CANCELLED:
                self._cancelled.discard(job_id)
                job.status = JobStatus.COMPLETED
                job.progress = 100
                job.completed_at = datetime.utcnow()

                # Get output file size
                if os.path.exists(job.output_path):
                    job.output_size = os.path.getsize(job.output_path)

                await self._notify_subscribers(job_id, {
                    "type": "complete",
                    "job_id": job_id,
                    "output_path": job.output_path,
                    "output_size": job.output_size
                })

        except ConversionCancelled:
            self._cancelled.discard(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.utcnow()
            await self._notify_subscribers(job_id, {
                "type": "cancelled",
                "job_id": job_id,
                "status": job.status.value
            })
        except Exception as e:
            self._cancelled.discard(job_id)
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = datetime.utcnow()

            await self._notify_subscribers(job_id, {
                "type": "error",
                "job_id": job_id,
                "error": str(e)
            })

        finally:
            # Only release lock if we acquired it
            if lock_acquired:
                lock_manager.release_lock(job.output_path)

            concurrency_manager.release(job_id)

            if job_id in self._cancel_events:
                del self._cancel_events[job_id]

            # Clean up temp directory if this was an archive extraction
            self._cleanup_temp_dir(job)


job_manager = JobManager(max_concurrent=settings.max_concurrent_jobs)
