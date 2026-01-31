import asyncio
import logging
import os
import resource
import shutil
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config import settings
from fastapi.concurrency import run_in_threadpool
from models import ConversionJob, ConversionMode, JobStatus
from services.archive import archive_service
from services.chd_metadata_store import chd_metadata_store
from services.chdman import ConversionCancelled, chdman_service
from services.concurrency_manager import concurrency_manager
from services.dolphin_tool import dolphin_tool_service
from services.lock_manager import lock_manager
from services.verification_store import verification_store
from utils.delete_plan import build_delete_plan
from utils.path_utils import is_within_configured_volumes, strip_archive_path

logger = logging.getLogger("chd.job_manager")


class JobManager:
    """Manages conversion job queue and execution."""

    def __init__(self, max_concurrent: int = 2, max_job_history: int = 500):
        self.jobs: OrderedDict[str, ConversionJob] = OrderedDict()
        self.max_concurrent = max(1, max_concurrent)
        self.max_job_history = max(0, max_job_history)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._cancelled: Set[str] = set()
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._delete_plans: Dict[str, Dict[str, object]] = {}
        self._last_progress_at: Dict[str, float] = {}
        self._last_progress_log_at: Dict[str, float] = {}
        self._last_stall_log_at: Dict[str, float] = {}
        self._pid_stats: Dict[int, Dict[str, int]] = {}
        self._last_output_size: Dict[str, int] = {}
        self._last_output_size_at: Dict[str, float] = {}
        self._running = False
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._debug_task: Optional[asyncio.Task] = None
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def create_job(
        self,
        file_path: str,
        mode: ConversionMode,
        output_dir: Optional[str] = None,
        output_path: Optional[str] = None,
        allow_overwrite: bool = False,
        filename_override: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        delete_snapshot: Optional[Dict[str, object]] = None,
    ) -> ConversionJob:
        """Create a new conversion job."""
        job_id = str(uuid.uuid4())[:8]
        filename = filename_override or os.path.basename(file_path)

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
            created_at=datetime.now(timezone.utc),
            output_path=output_path,
            allow_overwrite=allow_overwrite,
            compression=compression,
            delete_on_verify=delete_on_verify,
        )

        self.jobs[job_id] = job
        if delete_on_verify and delete_snapshot:
            self._delete_plans[job_id] = delete_snapshot
        ticket = concurrency_manager.reserve_ticket(job_id)
        self._queue.put_nowait((ticket, job_id))
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        self._last_progress_log_at[job_id] = now
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Queued job %s mode=%s input=%s output=%s overwrite=%s compression=%s",
                job_id,
                mode.value,
                file_path,
                output_path,
                allow_overwrite,
                compression,
            )
        await self._prune_jobs()
        return job

    async def create_batch_jobs(
        self,
        file_paths: List[str],
        mode: ConversionMode,
        output_dir: Optional[str] = None,
        compression: Optional[str] = None,
        delete_on_verify: bool = False,
        delete_snapshots: Optional[Dict[str, Dict[str, object]]] = None,
    ) -> List[ConversionJob]:
        """Create multiple conversion jobs."""
        jobs = []
        for fp in file_paths:
            snapshot = delete_snapshots.get(fp) if delete_snapshots else None
            job = await self.create_job(
                fp,
                mode,
                output_dir,
                compression=compression,
                delete_on_verify=delete_on_verify,
                delete_snapshot=snapshot,
            )
            jobs.append(job)
        return jobs

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def get_all_jobs(self) -> List[ConversionJob]:
        """Get all jobs."""
        return list(self.jobs.values())

    def get_active_job_candidates(self) -> List[Tuple[str, List[str]]]:
        """Return active job ids with their candidate paths (input/output)."""
        candidates: List[Tuple[str, List[str]]] = []
        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            candidates.append((job.id, self._candidate_paths(job)))
        return candidates

    @staticmethod
    def _normalize_path(path: str) -> Optional[Path]:
        try:
            return Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _track_candidate_paths(self, file_path: str) -> List[str]:
        if "::" in file_path:
            return []
        ext = Path(file_path).suffix.lower()
        if ext not in {".cue", ".gdi"}:
            return []
        if not os.path.isfile(file_path):
            return []
        try:
            plan = build_delete_plan(file_path)
        except Exception:
            return []
        source_real = os.path.realpath(file_path)
        tracks = []
        for path in plan.get("delete_paths", []):
            if path == source_real:
                continue
            if os.path.exists(path):
                tracks.append(path)
        return tracks

    def _candidate_paths(self, job: ConversionJob) -> List[str]:
        paths = []
        file_path = job.file_path
        if "::" in file_path:
            paths.append(file_path.split("::", 1)[0])
        else:
            paths.append(file_path)
            paths.extend(self._track_candidate_paths(file_path))
        if job.output_path:
            paths.append(job.output_path)
            if job.mode == ConversionMode.EXTRACTCD:
                paths.append(str(Path(job.output_path).with_suffix(".bin")))
        return paths

    def find_active_job_for_path(
        self, path: str, *, is_dir: bool = False
    ) -> Optional[ConversionJob]:
        """Return the active job using a path (input/output) or None."""
        target = self._normalize_path(path)
        if target is None:
            return None

        for job in self.jobs.values():
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return job
                if is_dir:
                    try:
                        cand_path.relative_to(target)
                        return job
                    except ValueError:
                        continue
        return None

    def _is_path_in_use_by_other_job(self, job_id: str, path: str) -> bool:
        target = self._normalize_path(path)
        if target is None:
            return False

        for job in self.jobs.values():
            if job.id == job_id:
                continue
            if job.status not in (JobStatus.QUEUED, JobStatus.PROCESSING):
                continue
            for candidate in self._candidate_paths(job):
                cand_path = self._normalize_path(candidate)
                if cand_path is None:
                    continue
                if cand_path == target:
                    return True
        return False

    async def _prune_jobs(self, *, exclude_id: Optional[str] = None):
        if self.max_job_history <= 0:
            return
        if len(self.jobs) <= self.max_job_history:
            return

        removable = []
        for job_id, job in self.jobs.items():
            if job_id == exclude_id:
                continue
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                removable.append(job_id)
            if len(self.jobs) - len(removable) <= self.max_job_history:
                break

        for job_id in removable:
            await self.delete_job(job_id)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a job."""
        job = self.jobs.get(job_id)
        if not job:
            return False

        if job.status == JobStatus.QUEUED:
            self._cancelled.add(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            concurrency_manager.release(job_id)
            asyncio.create_task(
                self._notify_subscribers(
                    job_id,
                    {"type": "cancelled", "job_id": job_id, "status": job.status.value},
                )
            )
            await self._cleanup_temp_dir(job)
            return True

        if job.status == JobStatus.PROCESSING:
            self._cancelled.add(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            job.message = "Cancelling..."
            asyncio.create_task(
                self._notify_subscribers(
                    job_id,
                    {
                        "type": "status",
                        "job_id": job_id,
                        "status": job.status.value,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )
            )
            return True
        return False

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job from the list."""
        if job_id in self.jobs:
            job = self.jobs[job_id]
            if job.status == JobStatus.PROCESSING:
                await self.cancel_job(job_id)
            self._cancelled.discard(job_id)
            self._delete_plans.pop(job_id, None)
            if job_id not in self._cancel_events:
                concurrency_manager.release(job_id)
            if job_id not in self._cancel_events:
                await self._cleanup_temp_dir(job)
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

    async def _cleanup_temp_dir(self, job: ConversionJob):
        if job.temp_dir:
            temp_dir = job.temp_dir
            try:
                if not self._is_safe_temp_dir(temp_dir):
                    logger.warning(
                        "Skipping cleanup for unsafe temp dir %s (job %s)",
                        temp_dir,
                        job.id,
                    )
                    return
                if await run_in_threadpool(os.path.isdir, temp_dir):
                    await run_in_threadpool(shutil.rmtree, temp_dir, ignore_errors=True)
            except Exception as cleanup_error:
                print(f"Failed to cleanup temp dir {temp_dir}: {cleanup_error}")
            finally:
                job.temp_dir = None

    def _is_safe_temp_dir(self, temp_dir: str) -> bool:
        if not temp_dir:
            return False
        try:
            resolved = Path(temp_dir).resolve(strict=False)
        except (OSError, RuntimeError):
            return False

        bases = []
        if settings.temp_dir:
            try:
                bases.append(Path(settings.temp_dir).resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                logger.debug(
                    "Skipping configured temp_dir %s due to resolution error: %s",
                    settings.temp_dir,
                    exc,
                )
        try:
            bases.append(Path(settings.data_dir).resolve(strict=False) / "temp")
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping data_dir temp base %s due to resolution error: %s",
                settings.data_dir,
                exc,
            )
        try:
            bases.append(Path(tempfile.gettempdir()).resolve(strict=False))
        except (OSError, RuntimeError) as exc:
            logger.debug(
                "Skipping system temp base due to resolution error: %s",
                exc,
            )

        for base in bases:
            try:
                resolved.relative_to(base)
                return True
            except ValueError:
                continue
        return False

    async def _notify_subscribers(self, job_id: str, data: dict):
        """Notify all subscribers of a job update."""
        if job_id in self._subscribers:
            for queue in self._subscribers[job_id]:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

    async def _set_job_message(self, job_id: str, message: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.message = message
        now = time.monotonic()
        self._last_progress_at[job_id] = now
        await self._notify_subscribers(
            job_id,
            {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
            },
        )

    async def _emit_extract_updates(
        self, job_id: str, archive_path: str, internal_path: str, task: asyncio.Task
    ):
        start = time.monotonic()
        while not task.done():
            job = self.jobs.get(job_id)
            if not job or job.status != JobStatus.PROCESSING:
                return
            elapsed = int(time.monotonic() - start)
            name = os.path.basename(internal_path)
            message = f"Extracting {name}... ({elapsed}s)"
            await self._set_job_message(job_id, message)
            await asyncio.sleep(2)

    async def process_queue(self):
        """Background task to process conversion queue."""
        if self._running:
            return
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())
        if settings.debug and settings.debug_heartbeat_interval > 0:
            self._debug_task = asyncio.create_task(self._debug_loop())
        await self._dispatcher_task

    async def _debug_loop(self):
        cleanup_counter = 0
        while self._running:
            try:
                await asyncio.sleep(settings.debug_heartbeat_interval)
                
                # Periodic stale lock cleanup (every 10 heartbeats = 5 minutes by default)
                cleanup_counter += 1
                if cleanup_counter >= 10:
                    cleanup_counter = 0
                    try:
                        removed = await run_in_threadpool(lock_manager.cleanup_stale_locks_periodic)
                        if removed > 0:
                            logger.info("Periodic cleanup removed %d stale lock file(s)", removed)
                    except Exception as e:
                        logger.warning("Periodic lock cleanup failed: %s", e)
                
                if not logger.isEnabledFor(logging.DEBUG):
                    continue

                jobs = list(self.jobs.values())
                status_counts = {
                    JobStatus.QUEUED: 0,
                    JobStatus.PROCESSING: 0,
                    JobStatus.COMPLETED: 0,
                    JobStatus.FAILED: 0,
                    JobStatus.CANCELLED: 0,
                }
                for job in jobs:
                    status_counts[job.status] = status_counts.get(job.status, 0) + 1

                subscriber_queues = sum(len(qs) for qs in self._subscribers.values())
                temp_dirs = sum(1 for job in jobs if job.temp_dir)

                usage = resource.getrusage(resource.RUSAGE_SELF)
                rss_raw = usage.ru_maxrss
                if sys.platform == "darwin":
                    rss_mb = rss_raw / (1024 * 1024)
                else:
                    rss_mb = rss_raw / 1024
                open_fds = None
                if os.path.exists("/proc/self/fd"):
                    try:
                        open_fds = len(os.listdir("/proc/self/fd"))
                    except OSError:
                        open_fds = None
                loadavg = None
                if hasattr(os, "getloadavg"):
                    try:
                        loadavg = os.getloadavg()
                    except OSError:
                        loadavg = None

                logger.debug(
                    "Heartbeat jobs=%d queued=%d processing=%d completed=%d failed=%d cancelled=%d "
                    "queue_size=%d semaphore=%s cancelled_set=%d subscribers=%d temp_dirs=%d "
                    "locks=%d tickets=%d active_chdman=%d rss_raw=%d rss_mb=%.1f open_fds=%s loadavg=%s",
                    len(jobs),
                    status_counts[JobStatus.QUEUED],
                    status_counts[JobStatus.PROCESSING],
                    status_counts[JobStatus.COMPLETED],
                    status_counts[JobStatus.FAILED],
                    status_counts[JobStatus.CANCELLED],
                    self._queue.qsize(),
                    getattr(self._semaphore, "_value", None),
                    len(self._cancelled),
                    subscriber_queues,
                    temp_dirs,
                    lock_manager.stats().get("locks"),
                    concurrency_manager.stats().get("tickets"),
                    len(chdman_service.active_pids()),
                    rss_raw,
                    rss_mb,
                    open_fds,
                    loadavg,
                )

                for job in jobs:
                    if job.status != JobStatus.PROCESSING:
                        continue
                    now = time.monotonic()
                    last_progress = self._last_progress_at.get(job.id, now)
                    idle_for = now - last_progress
                    output_size = None
                    output_idle = None
                    if job.output_path and os.path.exists(job.output_path):
                        try:
                            output_size = os.path.getsize(job.output_path)
                        except OSError:
                            output_size = None
                        if output_size is not None:
                            last_size = self._last_output_size.get(job.id)
                            last_size_at = self._last_output_size_at.get(job.id, now)
                            if last_size is None or output_size != last_size:
                                self._last_output_size[job.id] = output_size
                                self._last_output_size_at[job.id] = now
                            else:
                                output_idle = now - last_size_at
                    logger.debug(
                        "Processing job %s progress=%s idle=%.1fs output_size=%s output_idle=%s",
                        job.id,
                        job.progress,
                        idle_for,
                        output_size,
                        output_idle,
                    )

                if settings.debug_progress_timeout > 0:
                    now = time.monotonic()
                    for job in jobs:
                        if job.status != JobStatus.PROCESSING:
                            continue
                        output_size = None
                        output_idle = None
                        if job.output_path and os.path.exists(job.output_path):
                            try:
                                output_size = os.path.getsize(job.output_path)
                            except OSError:
                                output_size = None
                            if output_size is not None:
                                last_size = self._last_output_size.get(job.id)
                                last_size_at = self._last_output_size_at.get(
                                    job.id, now
                                )
                                if last_size is None or output_size != last_size:
                                    self._last_output_size[job.id] = output_size
                                    self._last_output_size_at[job.id] = now
                                else:
                                    output_idle = now - last_size_at
                        last_progress = self._last_progress_at.get(job.id, now)
                        idle_for = now - last_progress
                        if idle_for < settings.debug_progress_timeout:
                            continue
                        last_stall = self._last_stall_log_at.get(job.id, 0)
                        if now - last_stall < settings.debug_progress_timeout:
                            continue
                        self._last_stall_log_at[job.id] = now
                        logger.debug(
                            "Stalled job %s idle=%.1fs progress=%s message=%s input=%s output=%s "
                            "output_size=%s output_idle=%s started_at=%s",
                            job.id,
                            idle_for,
                            job.progress,
                            job.message,
                            job.file_path,
                            job.output_path,
                            output_size,
                            output_idle,
                            job.started_at,
                        )

                for pid in chdman_service.active_pids():
                    proc_io_path = f"/proc/{pid}/io"
                    proc_status_path = f"/proc/{pid}/status"
                    if not os.path.exists(proc_status_path):
                        self._pid_stats.pop(pid, None)
                        continue

                    rss_kb = None
                    threads = None
                    read_bytes = None
                    write_bytes = None

                    try:
                        with open(proc_status_path, "r", encoding="utf-8") as fh:
                            for line in fh:
                                if line.startswith("VmRSS:"):
                                    rss_kb = int(line.split()[1])
                                elif line.startswith("Threads:"):
                                    threads = int(line.split()[1])
                    except OSError:
                        pass

                    if os.path.exists(proc_io_path):
                        try:
                            with open(proc_io_path, "r", encoding="utf-8") as fh:
                                for line in fh:
                                    if line.startswith("read_bytes:"):
                                        read_bytes = int(line.split()[1])
                                    elif line.startswith("write_bytes:"):
                                        write_bytes = int(line.split()[1])
                        except OSError:
                            pass

                    prev = self._pid_stats.get(pid, {})
                    delta_read = None
                    delta_write = None
                    if read_bytes is not None and "read_bytes" in prev:
                        delta_read = read_bytes - prev["read_bytes"]
                    if write_bytes is not None and "write_bytes" in prev:
                        delta_write = write_bytes - prev["write_bytes"]

                    logger.debug(
                        "chdman pid=%s rss_kb=%s threads=%s read_bytes=%s(+%s) write_bytes=%s(+%s)",
                        pid,
                        rss_kb,
                        threads,
                        read_bytes,
                        delta_read,
                        write_bytes,
                        delta_write,
                    )

                    updated = {}
                    if read_bytes is not None:
                        updated["read_bytes"] = read_bytes
                    if write_bytes is not None:
                        updated["write_bytes"] = write_bytes
                    if updated:
                        self._pid_stats[pid] = updated
            except Exception as exc:
                logger.exception("Debug heartbeat error: %s", exc)

    async def _dispatcher_loop(self):
        """Dispatcher loop that starts jobs in FIFO order with concurrency control."""
        while self._running:
            try:
                _, job_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                job = self.jobs.get(job_id)
                if not job:
                    continue
                if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
                    self._cancelled.discard(job_id)
                    await self._cleanup_temp_dir(job)
                    continue
                await self._semaphore.acquire()
                try:
                    asyncio.create_task(self._run_job(job_id))
                except Exception:
                    self._semaphore.release()
                    raise
            except Exception as e:
                logger.exception("Dispatcher error: %s", e)
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str):
        try:
            await self._process_job(job_id)
        finally:
            self._semaphore.release()

    async def _process_job(self, job_id: str):
        """Process a single conversion job."""
        job = self.jobs.get(job_id)
        if not job:
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Starting job %s status=%s input=%s output=%s",
                job_id,
                job.status.value,
                job.file_path,
                job.output_path,
            )

        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            self._cancelled.discard(job_id)
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        cancel_event = asyncio.Event()
        self._cancel_events[job_id] = cancel_event
        if job_id in self._cancelled or job.status == JobStatus.CANCELLED:
            cancel_event.set()
            self._cancelled.discard(job_id)
            if not job.completed_at:
                job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
            await self._cleanup_temp_dir(job)
            del self._cancel_events[job_id]
            await self._prune_jobs(exclude_id=job_id)
            return

        slot_acquired = await concurrency_manager.acquire(
            job_id, cancel_event=cancel_event
        )
        if not slot_acquired:
            if job.status != JobStatus.CANCELLED:
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {"type": "cancelled", "job_id": job_id, "status": job.status.value},
                )
            await self._cleanup_temp_dir(job)
            concurrency_manager.release(job_id)
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            await self._prune_jobs(exclude_id=job_id)
            return

        # Try to acquire lock for the output file (prevents race conditions)
        lock_acquired = lock_manager.acquire_lock(
            job.output_path, allow_existing=job.allow_overwrite
        )
        if not lock_acquired:
            # Could not acquire lock - either file exists or is being converted
            # Check current status to provide better error message
            file_exists, is_locked = lock_manager.check_file_status(job.output_path)
            job.status = JobStatus.FAILED
            if is_locked:
                job.error_message = (
                    "Another job is already converting to this output file"
                )
            elif file_exists:
                job.error_message = "Output CHD file already exists"
            else:
                job.error_message = "Could not acquire lock for output file"
            job.completed_at = datetime.now(timezone.utc)

            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": job.error_message}
            )
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            concurrency_manager.release(job_id)
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            return

        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Job %s acquired locks and started processing", job_id)

        await self._notify_subscribers(
            job_id,
            {
                "type": "status",
                "job_id": job_id,
                "status": job.status.value,
                "progress": 0,
            },
        )

        try:
            input_path = job.file_path
            if "::" in job.file_path:
                extract_start = time.monotonic()
                archive_path, internal_path = job.file_path.split("::", 1)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Job %s extracting archive %s member %s",
                        job_id,
                        archive_path,
                        internal_path,
                    )
                extract_task = asyncio.create_task(
                    run_in_threadpool(
                        archive_service.extract_file, archive_path, internal_path
                    )
                )
                extract_status_task = asyncio.create_task(
                    self._emit_extract_updates(
                        job_id, archive_path, internal_path, extract_task
                    )
                )
                input_path, temp_dir = await extract_task
                extract_status_task.cancel()
                try:
                    await extract_status_task
                except asyncio.CancelledError:
                    pass
                job.temp_dir = temp_dir
                await run_in_threadpool(
                    archive_service.extract_related_files,
                    archive_path,
                    internal_path,
                    temp_dir,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    extracted_size = None
                    try:
                        extracted_size = os.path.getsize(input_path)
                    except OSError:
                        pass
                    logger.debug(
                        "Job %s extracted to %s size=%s in %.2fs",
                        job_id,
                        input_path,
                        extracted_size,
                        time.monotonic() - extract_start,
                    )
                if cancel_event.is_set():
                    raise ConversionCancelled("Conversion cancelled")

            if job.allow_overwrite and os.path.exists(job.output_path):
                if not os.path.isfile(job.output_path):
                    raise RuntimeError("Output path exists and is not a file")
                os.remove(job.output_path)
                await verification_store.clear(job.output_path)
                if job.mode.value == "extractcd":
                    bin_path = str(Path(job.output_path).with_suffix(".bin"))
                    if os.path.isfile(bin_path):
                        os.remove(bin_path)

            _convert_service = (
                dolphin_tool_service
                if job.mode.value.startswith("dolphin_")
                else chdman_service
            )
            async for update in _convert_service.convert(
                input_path,
                job.output_path,
                job.mode.value,
                compression=job.compression,
                cancel_event=cancel_event,
            ):
                if cancel_event.is_set():
                    continue
                job.progress = update["progress"]
                job.message = update["message"]
                now = time.monotonic()
                self._last_progress_at[job_id] = now
                if logger.isEnabledFor(logging.DEBUG):
                    last_log = self._last_progress_log_at.get(job_id, 0)
                    if now - last_log >= settings.debug_progress_interval:
                        self._last_progress_log_at[job_id] = now
                        logger.debug(
                            "Job %s progress=%s message=%s",
                            job_id,
                            job.progress,
                            job.message,
                        )

                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "progress",
                        "job_id": job_id,
                        "progress": job.progress,
                        "message": job.message,
                    },
                )

            if job.status != JobStatus.CANCELLED:
                self._cancelled.discard(job_id)
                job.progress = 100

                # Get output file size
                if os.path.exists(job.output_path):
                    if job.mode.value == "extractcd":
                        cue_size = 0
                        bin_size = 0
                        try:
                            cue_size = os.path.getsize(job.output_path)
                        except OSError:
                            pass
                        try:
                            bin_path = str(Path(job.output_path).with_suffix(".bin"))
                            if os.path.exists(bin_path):
                                bin_size = os.path.getsize(bin_path)
                        except OSError:
                            pass
                        total_size = cue_size + bin_size
                        job.output_size = total_size if total_size > 0 else None
                    else:
                        job.output_size = os.path.getsize(job.output_path)

                verified = False
                source_deleted = False
                if job.delete_on_verify:
                    if job.mode.value.startswith("extract"):
                        raise RuntimeError(
                            "Delete-on-verify is only supported for create/copy modes"
                        )
                    if cancel_event.is_set():
                        raise ConversionCancelled("Conversion cancelled")

                    job.message = "Verifying output..."
                    await self._notify_subscribers(
                        job_id,
                        {
                            "type": "progress",
                            "job_id": job_id,
                            "progress": job.progress,
                            "message": job.message,
                        },
                    )

                    _verify_service = (
                        dolphin_tool_service
                        if job.mode.value.startswith("dolphin_")
                        else chdman_service
                    )
                    verify_result = await _verify_service.verify(
                        job.output_path
                    )
                    if not verify_result.get("valid"):
                        raise RuntimeError(
                            f"Verification failed: {verify_result.get('message')}"
                        )

                    verified = True
                    await verification_store.mark_verified(
                        job.output_path, source_path=job.file_path
                    )

                    if cancel_event.is_set():
                        job.message = "Verification complete. Delete skipped (cancelled)."
                        await self._notify_subscribers(
                            job_id,
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "progress": job.progress,
                                "message": job.message,
                            },
                        )
                    else:
                        delete_label = (
                            "source archive"
                            if "::" in job.file_path
                            else "source"
                        )
                        job.message = (
                            f"Verification complete. Deleting {delete_label}..."
                        )
                        await self._notify_subscribers(
                            job_id,
                            {
                                "type": "progress",
                                "job_id": job_id,
                                "progress": job.progress,
                                "message": job.message,
                            },
                        )

                        snapshot = self._delete_plans.get(job_id)
                        if not snapshot or not snapshot.get("paths"):
                            raise RuntimeError(
                                "Delete plan snapshot missing; refusing to delete"
                            )

                        expected_paths = list(snapshot.get("paths", []))
                        expected_set = set(expected_paths)
                        fingerprints = snapshot.get("fingerprints") or {}

                        current_plan = await run_in_threadpool(
                            build_delete_plan, job.file_path
                        )
                        if (
                            current_plan.get("errors")
                            or current_plan.get("unsafe_paths")
                            or current_plan.get("missing_paths")
                        ):
                            raise RuntimeError(
                                "Delete plan no longer safe; refusing to delete"
                            )
                        current_set = set(current_plan.get("delete_paths", []))
                        if current_set != expected_set:
                            raise RuntimeError(
                                "Delete plan changed; refusing to delete"
                            )

                        output_real = os.path.realpath(job.output_path)
                        source_for_delete = (
                            strip_archive_path(job.file_path)
                            if "::" in job.file_path
                            else job.file_path
                        )
                        source_real = os.path.realpath(source_for_delete)
                        delete_order = [
                            p
                            for p in expected_paths
                            if os.path.realpath(p) != source_real
                        ]
                        if source_real in expected_set:
                            delete_order.append(source_real)
                        else:
                            delete_order = expected_paths

                        for path in expected_paths:
                            _, is_locked = lock_manager.check_file_status(path)
                            if is_locked:
                                raise RuntimeError(
                                    "Delete path is locked by an active conversion"
                                )
                            within_volumes = await run_in_threadpool(
                                is_within_configured_volumes,
                                path,
                                treat_archives=False,
                            )
                            if not within_volumes:
                                raise RuntimeError(
                                    "Delete path outside configured volumes; refusing to delete"
                                )
                            if self._is_path_in_use_by_other_job(job_id, path):
                                raise RuntimeError(
                                    "Delete path is still in use by another job"
                                )
                            if os.path.islink(path):
                                raise RuntimeError(
                                    "Delete path is a symlink; refusing to delete"
                                )
                            try:
                                st = os.stat(path, follow_symlinks=False)
                            except FileNotFoundError:
                                raise RuntimeError(
                                    "Delete path no longer exists; refusing to delete"
                                )
                            if not os.path.isfile(path):
                                raise RuntimeError(
                                    "Delete path is not a file; refusing to delete"
                                )
                            if os.path.realpath(path) == output_real:
                                raise RuntimeError(
                                    "Delete path matches output path; refusing to delete"
                                )
                            fp = fingerprints.get(path)
                            if not fp:
                                raise RuntimeError(
                                    "Delete fingerprint missing; refusing to delete"
                                )
                            mtime_ns = int(
                                getattr(
                                    st,
                                    "st_mtime_ns",
                                    int(st.st_mtime * 1_000_000_000),
                                )
                            )
                            if (
                                int(fp.get("size", -1)) != int(st.st_size)
                                or int(fp.get("mtime_ns", -1)) != mtime_ns
                                or int(fp.get("inode", -1))
                                != int(getattr(st, "st_ino", 0))
                                or int(fp.get("device", -1))
                                != int(getattr(st, "st_dev", 0))
                            ):
                                raise RuntimeError(
                                    "Delete path fingerprint mismatch; refusing to delete"
                                )

                        for path in delete_order:
                            await run_in_threadpool(os.remove, path)

                        source_deleted = True

                        if job.file_path.lower().endswith(".chd"):
                            await verification_store.clear(job.file_path)
                            await chd_metadata_store.clear(job.file_path)

                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(timezone.utc)
                await self._notify_subscribers(
                    job_id,
                    {
                        "type": "complete",
                        "job_id": job_id,
                        "output_path": job.output_path,
                        "output_size": job.output_size,
                        "verified": verified,
                        "source_deleted": source_deleted,
                    },
                )

        except ConversionCancelled:
            self._cancelled.discard(job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            await self._notify_subscribers(
                job_id,
                {"type": "cancelled", "job_id": job_id, "status": job.status.value},
            )
        except Exception as e:
            self._cancelled.discard(job_id)
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = datetime.now(timezone.utc)

            await self._notify_subscribers(
                job_id, {"type": "error", "job_id": job_id, "error": str(e)}
            )

        finally:
            # Only release lock if we acquired it
            if lock_acquired:
                lock_manager.release_lock(job.output_path)

            concurrency_manager.release(job_id)

            self._delete_plans.pop(job_id, None)
            if job_id in self._cancel_events:
                del self._cancel_events[job_id]
            self._last_progress_at.pop(job_id, None)
            self._last_progress_log_at.pop(job_id, None)
            self._last_stall_log_at.pop(job_id, None)
            self._last_output_size.pop(job_id, None)
            self._last_output_size_at.pop(job_id, None)

            # Clean up temp directory if this was an archive extraction
            await self._cleanup_temp_dir(job)
            await self._prune_jobs(exclude_id=job_id)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Job %s finished status=%s", job_id, job.status.value)


job_manager = JobManager(
    max_concurrent=settings.max_concurrent_jobs,
    max_job_history=settings.max_job_history,
)
