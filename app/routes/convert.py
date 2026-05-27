import asyncio
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
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


def _get_output_path(mode, input_path, output_dir, *, treat_as_stem=False):
    return registry.for_mode(mode).output_path(
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


@dataclass
class JobPlan:
    """Resolved per-file plan shared by single and batch job creation."""

    file_path: str
    output_path: str
    base_output_path: str
    allow_overwrite: bool
    display_filename: str | None
    delete_snapshot: dict | None
    priority: int


class SkipReason(Enum):
    """Why ``plan_job`` could not turn a file into a job.

    Single-job callers translate each reason into the matching
    ``HTTPException``; batch callers append the file to ``skipped`` and
    continue. The two behaviours are deliberately different (see
    ``_SKIP_HTTP``).
    """

    ARCHIVE_INPUT_NOT_ALLOWED = "archive_input_not_allowed"
    ARCHIVE_NOT_FOUND = "archive_not_found"
    OUTPUT_EXISTS = "output_exists"
    OUTPUT_LOCKED = "output_locked"
    FILE_NOT_FOUND = "file_not_found"
    EXTRACT_COPY_REQUIRES_CHD = "extract_copy_requires_chd"
    CREATE_REQUIRES_NON_CHD = "create_requires_non_chd"
    DOLPHIN_BAD_EXTENSION = "dolphin_bad_extension"
    Z3DS_BAD_EXTENSION = "z3ds_bad_extension"
    DOLPHIN_SAME_PATH = "dolphin_same_path"


class SkipFile(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raised by ``plan_job`` when a file cannot be planned into a job."""

    def __init__(self, reason: SkipReason):
        self.reason = reason
        super().__init__(reason.value)


class DeleteSnapshotError(Exception):
    """Raised when building the delete-on-verify snapshot fails.

    Unlike ``SkipFile`` this aborts both single and batch creation; each
    caller formats its own (differing) ``detail`` string.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# Single-job status/detail for each skip reason. Must match the exact strings
# and codes the pre-Phase-4 ``create_job`` raised. Batch callers ignore this
# table and simply skip.
_SKIP_HTTP: dict[SkipReason, tuple[int, str]] = {
    SkipReason.ARCHIVE_INPUT_NOT_ALLOWED: (
        400,
        "Archive inputs are not supported for"
        " extract/copy/dolphin/z3ds_compress modes",
    ),
    SkipReason.ARCHIVE_NOT_FOUND: (404, "Archive not found"),
    SkipReason.OUTPUT_EXISTS: (409, "Output file already exists"),
    SkipReason.OUTPUT_LOCKED: (409, "Output file is currently being converted"),
    SkipReason.FILE_NOT_FOUND: (404, "File not found"),
    SkipReason.EXTRACT_COPY_REQUIRES_CHD: (
        400,
        "Extract/copy modes require .chd input files",
    ),
    SkipReason.CREATE_REQUIRES_NON_CHD: (
        400,
        "Create modes require non-CHD input files",
    ),
    SkipReason.DOLPHIN_BAD_EXTENSION: (
        400,
        "Dolphin modes require GameCube/Wii disc images "
        "(.iso, .gcz, .wia, .rvz, .wbfs)",
    ),
    SkipReason.Z3DS_BAD_EXTENSION: (
        400,
        "z3ds_compress mode requires Nintendo 3DS ROM files (.cci, .cia, .3ds)",
    ),
    SkipReason.DOLPHIN_SAME_PATH: (
        400,
        "Output path matches input; overwriting would delete the source file",
    ),
}


async def plan_job(
    file_path: str,
    *,
    spec,
    mode: str,
    output_dir: str | None,
    duplicate_action: DuplicateAction,
    delete_on_verify: bool,
) -> JobPlan:
    """Resolve one input file into a concrete ``JobPlan``.

    Holds the per-file validation / output-path / duplicate-handling pipeline
    shared by ``create_job`` and ``create_batch_jobs``. Validation failures are
    signalled via ``SkipFile`` (the caller decides raise-vs-skip);
    delete-snapshot failures raise ``DeleteSnapshotError``.

    Request-level concerns (compression validation, volume containment, queue
    backpressure, the cross-file archive multi-select guard, the batch dedup
    pass) stay in the endpoints, not here.
    """
    is_dolphin = spec.tool_id == "dolphin"
    is_z3ds = spec.tool_id == "z3ds"

    archive_source_dir = None  # Directory where the archive is located (for output)
    output_path = None
    base_output_path = None
    output_exists = False

    display_filename = None

    # Handle archive files
    if "::" in file_path:
        if not spec.allows_archive_input:
            raise SkipFile(SkipReason.ARCHIVE_INPUT_NOT_ALLOWED)
        archive_path, internal_path = file_path.split("::", 1)
        archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive
        display_filename = os.path.basename(internal_path)

        if not os.path.isfile(archive_path):
            raise SkipFile(SkipReason.ARCHIVE_NOT_FOUND)

        # Calculate output path before extraction to avoid unnecessary work
        effective_output_dir = output_dir or archive_source_dir
        output_stem = archive_service._output_stem_for_member(internal_path)
        output_path = _get_output_path(
            mode, output_stem, effective_output_dir, treat_as_stem=True,
        )
        base_output_path = output_path

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if duplicate_action == DuplicateAction.SKIP:
                raise SkipFile(SkipReason.OUTPUT_EXISTS)
            if duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise SkipFile(SkipReason.OUTPUT_LOCKED)
            elif duplicate_action == DuplicateAction.RENAME:
                if mode == "extractcd":
                    output_path = get_unique_output_path_for_extractcd(output_path)
                else:
                    output_path = get_unique_output_path(output_path)

    if "::" not in file_path and not os.path.isfile(file_path):
        raise SkipFile(SkipReason.FILE_NOT_FOUND)

    if (
        spec.tool_id == "chdman" and spec.kind in (ModeKind.EXTRACT, ModeKind.COPY)
    ) and not file_path.lower().endswith(".chd"):
        raise SkipFile(SkipReason.EXTRACT_COPY_REQUIRES_CHD)

    if spec.kind == ModeKind.CREATE and file_path.lower().endswith(".chd"):
        raise SkipFile(SkipReason.CREATE_REQUIRES_NON_CHD)

    if is_dolphin:
        ext = Path(file_path).suffix.lower()
        if ext not in spec.input_extensions:
            raise SkipFile(SkipReason.DOLPHIN_BAD_EXTENSION)

    if is_z3ds:
        ext = Path(file_path).suffix.lower()
        if ext not in spec.input_extensions:
            raise SkipFile(SkipReason.Z3DS_BAD_EXTENSION)

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
            if duplicate_action == DuplicateAction.SKIP:
                raise SkipFile(SkipReason.OUTPUT_EXISTS)
            if duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise SkipFile(SkipReason.OUTPUT_LOCKED)
            elif duplicate_action == DuplicateAction.RENAME:
                if mode == "extractcd":
                    output_path = get_unique_output_path_for_extractcd(output_path)
                else:
                    output_path = get_unique_output_path(output_path)

    if (
        is_dolphin
        and duplicate_action == DuplicateAction.OVERWRITE
        and output_path
        and _is_same_path(output_path, file_path)
    ):
        raise SkipFile(SkipReason.DOLPHIN_SAME_PATH)

    allow_overwrite = (
        duplicate_action == DuplicateAction.OVERWRITE and output_exists
    )

    delete_snapshot = None
    if delete_on_verify:
        try:
            delete_snapshot = await run_in_threadpool(
                build_delete_snapshot, file_path,
            )
        except ValueError as exc:
            raise DeleteSnapshotError(str(exc)) from None

    return JobPlan(
        file_path=file_path,
        output_path=output_path,
        base_output_path=base_output_path or output_path,
        allow_overwrite=allow_overwrite,
        display_filename=display_filename,
        delete_snapshot=delete_snapshot,
        priority=_priority(_input_extension(file_path)),
    )


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

    try:
        plan = await plan_job(
            request.file_path,
            spec=spec,
            mode=mode,
            output_dir=output_dir,
            duplicate_action=request.duplicate_action,
            delete_on_verify=request.delete_on_verify,
        )
    except SkipFile as skip:
        status_code, detail = _SKIP_HTTP[skip.reason]
        raise HTTPException(status_code=status_code, detail=detail) from None
    except DeleteSnapshotError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Delete-on-verify blocked: {exc.message}",
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
            plan.file_path,
            request.mode,
            output_path=plan.output_path,
            allow_overwrite=plan.allow_overwrite,
            filename_override=plan.display_filename,
            compression=compression,
            delete_on_verify=request.delete_on_verify,
            delete_snapshot=plan.delete_snapshot,
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

    skipped = []
    candidates: list[JobPlan] = []

    for file_path in request.file_paths:
        try:
            plan = await plan_job(
                file_path,
                spec=spec,
                mode=mode,
                output_dir=output_dir,
                duplicate_action=request.duplicate_action,
                delete_on_verify=request.delete_on_verify,
            )
        except SkipFile:
            skipped.append(file_path)
            continue
        except DeleteSnapshotError as exc:
            raise HTTPException(
                status_code=400,
                # nosemgrep: python.django.security.injection.tainted-sql-string.tainted-sql-string
                detail=f"Delete-on-verify blocked for {file_path}: {exc.message}",
            ) from None
        candidates.append(plan)

    # Collapse multiple inputs that resolve to the same output (e.g. a .cue and
    # its .bin) down to the highest-priority one. No single-job analog.
    selected: dict[str, JobPlan] = {}
    order = []
    for plan in candidates:
        key = plan.base_output_path
        existing = selected.get(key)
        if not existing:
            selected[key] = plan
            order.append(key)
            continue
        if plan.priority > existing.priority:
            selected[key] = plan

    job_specs = []
    for key in order:
        plan = selected[key]
        job_specs.append(
            {
                "file_path": plan.file_path,
                "output_path": plan.output_path,
                "allow_overwrite": plan.allow_overwrite,
                "filename_override": plan.display_filename,
                "delete_snapshot": plan.delete_snapshot,
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
