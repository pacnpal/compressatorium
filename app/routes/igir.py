"""API routes for igir ROM collection management."""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from fastapi import APIRouter, HTTPException, Query, Request
from models import (
    DatFileEntry,
    IgirCommand,
    IgirFeatureEventRequest,
    IgirJob,
    IgirJobCreateRequest,
    IgirQuickSetupRequest,
    IgirValidationResult,
    JobStatus,
)
from services.igir import igir_service
from services.job_manager import QueueBackpressureError, job_manager
from sse_starlette.sse import EventSourceResponse
from utils.path_utils import is_within_configured_volumes

router = APIRouter()
logger = logging.getLogger("chd.igir.routes")

_AUTO_SETUP_TOKEN_RE = re.compile(r"[a-z0-9]+")
_AUTO_SETUP_STOPWORDS = {
    "rom",
    "roms",
    "game",
    "games",
    "set",
    "sets",
    "collection",
    "collections",
    "dump",
    "dumps",
    "disc",
    "discs",
    "backup",
    "backups",
}
_SUPPORTED_FEATURE_EVENTS = {
    "igir_autoconfig_applied",
    "igir_autoconfig_executed",
}
_feature_event_counts: dict[str, int] = {}
_feature_event_lock = threading.Lock()
_TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}
_TERMINAL_EVENT_TYPES = {"complete", "error", "cancelled"}
_TERMINAL_SNAPSHOT_LOOKBACK_SECONDS = 5


def _tokenize_auto_setup_text(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in _AUTO_SETUP_TOKEN_RE.findall(value.lower()):
        if len(token) < 2 or token in _AUTO_SETUP_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _build_auto_setup_tokens(input_paths: list[str]) -> set[str]:
    tokens: set[str] = set()
    for input_path in input_paths:
        path = Path(input_path)
        tokens.update(_tokenize_auto_setup_text(path.name))
        parent_name = path.parent.name
        if parent_name:
            tokens.update(_tokenize_auto_setup_text(parent_name))
    return tokens


def _recommend_dat_paths(
    dat_entries: list[DatFileEntry], input_tokens: set[str], limit: int = 6,
) -> list[str]:
    if not dat_entries:
        return []

    scored: list[tuple[int, str]] = []
    for entry in dat_entries:
        stem = Path(entry.name).stem
        dat_tokens = _tokenize_auto_setup_text(stem)
        overlap = input_tokens & dat_tokens
        if not overlap:
            continue
        score = sum(max(2, len(token)) for token in overlap)
        scored.append((score, entry.path))

    if scored:
        scored.sort(key=lambda item: (-item[0], item[1].lower()))
        return [path for _, path in scored[:limit]]

    # Fallback: no lexical match, return a small deterministic slice.
    fallback = sorted((entry.path for entry in dat_entries), key=str.lower)
    return fallback[: min(3, len(fallback))]


def _recommend_output_path(first_input_path: str) -> str:
    source = Path(first_input_path)
    if _is_glob_like(first_input_path):
        anchor = source.parent if str(source.parent) not in ("", ".") else source
        while anchor.name and _is_glob_like(anchor.name):
            anchor = anchor.parent
        anchor_name = anchor.name or "collection"
        candidate = anchor / f"{anchor_name}_organized"
    elif source.exists() and source.is_dir():
        # Keep recommendations inside the selected input tree.
        anchor_name = source.name or "collection"
        candidate = source / f"{anchor_name}_organized"
    elif source.exists() and source.is_file():
        anchor = source.parent
        anchor_name = anchor.name or source.stem or "collection"
        candidate = anchor / f"{anchor_name}_organized"
    elif source.name:
        candidate = source / f"{source.name}_organized"
    else:
        candidate = source / "organized"

    if str(candidate) == first_input_path:
        fallback_anchor = source.parent if source.parent != source else source
        fallback_name = fallback_anchor.name or "collection"
        candidate = fallback_anchor / f"{fallback_name}_organized"
    return str(candidate)


def _nearest_existing_parent(path: str) -> Path:
    current = Path(path).resolve(strict=False)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _build_workflow_templates(
    output_path: str,
    recommended_dats: list[str],
) -> list[dict[str, object]]:
    has_dats = len(recommended_dats) > 0
    dat_paths = recommended_dats if has_dats else []

    return [
        {
            "id": "first_sort",
            "label": "First-Time Sort",
            "description": "Copy, zip, and test into an organized collection",
            "commands": ["copy", "zip", "test"],
            "output_path": output_path,
            "dat_paths": dat_paths,
            "dir_dat_name": True,
            "requires_dats": True,
            "destructive": False,
            "single": False,
        },
        {
            "id": "merge_new",
            "label": "Merge New Into Golden Set",
            "description": "Move new ROMs into existing collection and clean unknowns",
            "commands": ["move", "zip", "test", "clean", "report"],
            "output_path": output_path,
            "dat_paths": dat_paths,
            "dir_dat_name": True,
            "requires_dats": True,
            "destructive": True,
            "single": False,
        },
        {
            "id": "flash_cart_1g1r",
            "label": "Flash Cart 1G1R",
            "description": "Copy one preferred ROM per game for flash carts",
            "commands": ["copy", "extract", "test"],
            "output_path": output_path,
            "dat_paths": dat_paths,
            "dir_letter": True,
            "single": True,
            "prefer_language": ["EN"],
            "prefer_region": ["USA", "WORLD", "EUR", "JPN"],
            "prefer_verified": True,
            "prefer_good": True,
            "no_bios": True,
            "requires_dats": True,
            "destructive": False,
        },
        {
            "id": "report_missing",
            "label": "Report + Fixdat",
            "description": "Generate collection report and fixdat for missing titles",
            "commands": ["report", "fixdat"],
            "dat_paths": dat_paths,
            "requires_dats": True,
            "destructive": False,
            "single": False,
        },
        {
            "id": "clean_preview",
            "label": "Clean Dry Run",
            "description": "Preview unknown files that would be removed by clean",
            "commands": ["clean", "report"],
            "output_path": output_path,
            "dat_paths": dat_paths,
            "clean_dry_run": True,
            "requires_dats": True,
            "destructive": False,
            "single": False,
        },
        {
            "id": "playlist_only",
            "label": "Playlist Generation",
            "description": "Create playlists for multi-disc games",
            "commands": ["playlist"],
            "requires_dats": False,
            "destructive": False,
            "single": False,
        },
        {
            "id": "mame_rebuild",
            "label": "MAME Rebuild",
            "description": "Rebuild a MAME set using split merge mode",
            "commands": ["copy", "zip", "test"],
            "output_path": output_path,
            "dat_paths": dat_paths,
            "merge_roms": "split",
            "requires_dats": True,
            "destructive": False,
            "single": False,
        },
    ]


def _select_workflow(
    workflows: list[dict[str, object]],
    goal: str | None,
) -> dict[str, object]:
    if goal:
        wanted = goal.strip().lower()
        for workflow in workflows:
            if str(workflow.get("id", "")).lower() == wanted:
                return workflow
    for workflow in workflows:
        if workflow.get("id") == "first_sort":
            return workflow
    return workflows[0] if workflows else {}


def _record_feature_event(event: str, value: int = 1) -> int:
    safe_value = value if value > 0 else 1
    with _feature_event_lock:
        _feature_event_counts[event] = _feature_event_counts.get(event, 0) + safe_value
        return _feature_event_counts[event]


def _path_overlaps(path_a: str, path_b: str) -> bool:
    try:
        a = Path(path_a).resolve(strict=False)
        b = Path(path_b).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _is_glob_like(path: str) -> bool:
    return any(ch in path for ch in ("*", "?", "[", "]"))


def _event_type_for_terminal_status(status: JobStatus) -> str:
    if status == JobStatus.COMPLETED:
        return "complete"
    if status == JobStatus.FAILED:
        return "error"
    if status == JobStatus.CANCELLED:
        return "cancelled"
    return "status"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _should_emit_terminal_snapshot(
    job: IgirJob,
    previous_status: JobStatus | None,
    connection_started_at: datetime,
) -> bool:
    if previous_status is not None:
        return previous_status not in _TERMINAL_JOB_STATUSES

    reference_time = _as_utc(job.completed_at) or _as_utc(job.created_at)
    if reference_time is None:
        return False

    lookback_cutoff = connection_started_at - timedelta(
        seconds=_TERMINAL_SNAPSHOT_LOOKBACK_SECONDS,
    )
    return reference_time >= lookback_cutoff


# ──────────────────────────── Job CRUD ────────────────────────────


@router.post("/igir/jobs", response_model=IgirJob)
async def create_igir_job(request: IgirJobCreateRequest):
    """Create a new igir ROM management job."""
    # Validate the request first
    validation = igir_service.validate_request(request)
    if not validation.valid:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid igir job request",
                "errors": validation.errors,
                "warnings": validation.warnings,
                "command_preview": validation.command_preview,
            },
        )

    try:
        job = await job_manager.create_igir_job(request)
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc
    return job


@router.get("/igir/jobs", response_model=list[IgirJob])
async def list_igir_jobs():
    """List all igir jobs."""
    return job_manager.get_all_igir_jobs()


@router.get("/igir/jobs/events")
async def igir_job_events():
    """SSE endpoint for all igir job progress updates."""

    async def event_generator():
        queues: dict[str, asyncio.Queue] = {}
        emitted_terminal_job_ids: set[str] = set()
        last_seen_statuses: dict[str, JobStatus] = {}
        connection_started_at = datetime.now(timezone.utc)
        try:
            while True:
                try:
                    jobs = job_manager.get_all_igir_jobs()
                    active_job_ids = {job.id for job in jobs}
                    emitted_terminal_job_ids.intersection_update(active_job_ids)
                    last_seen_statuses = {
                        job_id: status
                        for job_id, status in last_seen_statuses.items()
                        if job_id in active_job_ids
                    }

                    # Subscribe to active jobs and emit synthetic terminal snapshots
                    # only when a job newly transitions to terminal (or was just
                    # created/completed around connection start).
                    for job in jobs:
                        previous_status = last_seen_statuses.get(job.id)
                        if job.id not in queues and job.status in (
                            JobStatus.QUEUED,
                            JobStatus.PROCESSING,
                        ):
                            queues[job.id] = job_manager.subscribe_igir(job.id)
                            last_seen_statuses[job.id] = job.status
                            continue

                        if (
                            job.status in _TERMINAL_JOB_STATUSES
                            and job.id not in queues
                            and job.id not in emitted_terminal_job_ids
                            and _should_emit_terminal_snapshot(
                                job, previous_status, connection_started_at,
                            )
                        ):
                            event_type = _event_type_for_terminal_status(job.status)
                            emitted_terminal_job_ids.add(job.id)
                            payload = {
                                "type": event_type,
                                "job_id": job.id,
                                "status": job.status.value,
                                "progress": job.progress,
                                "message": job.message,
                                "phase": job.phase,
                                "job": job.model_dump(mode="json"),
                            }
                            yield {
                                "event": event_type,
                                "data": json.dumps(payload),
                            }

                        last_seen_statuses[job.id] = job.status

                    # Check all queues for updates
                    for job_id, queue in list(queues.items()):
                        try:
                            update = queue.get_nowait()
                            job = job_manager.get_igir_job(job_id)
                            if job is not None:
                                update = {
                                    **update,
                                    "job": job.model_dump(mode="json"),
                                }
                                if job.status in _TERMINAL_JOB_STATUSES:
                                    emitted_terminal_job_ids.add(job.id)

                            event_type = update.get("type", "progress")
                            yield {
                                "event": event_type,
                                "data": json.dumps(update),
                            }

                            # Unsubscribe if job is done
                            if event_type in _TERMINAL_EVENT_TYPES:
                                job_manager.unsubscribe_igir(job_id, queue)
                                del queues[job_id]

                        except asyncio.QueueEmpty:
                            pass

                    await asyncio.sleep(0.1)

                except Exception:
                    await asyncio.sleep(1)
        finally:
            for job_id, queue in list(queues.items()):
                job_manager.unsubscribe_igir(job_id, queue)

    return EventSourceResponse(event_generator())


@router.delete("/igir/jobs/completed")
async def delete_completed_igir_jobs(request: Request):
    """Delete all completed, failed, and cancelled igir jobs."""
    confirmation = request.headers.get("x-chd-action-confirm", "")
    if confirmation != "clear-completed-igir-jobs":
        raise HTTPException(
            status_code=400,
            detail="Missing confirmation header for clear-completed-igir-jobs action",
        )

    deleted_ids = await job_manager.clear_completed_igir_jobs()
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Clear completed igir jobs requested from %s; deleted=%d",
        client_host,
        len(deleted_ids),
    )
    return {"deleted": deleted_ids, "count": len(deleted_ids)}


@router.get("/igir/jobs/stuck-status")
async def igir_stuck_status():
    """Check if the igir job queue is stuck."""
    return job_manager.igir_stuck_status()


@router.post("/igir/jobs/recover")
async def recover_igir_jobs():
    """Attempt to recover igir jobs from a stuck state."""
    result = await job_manager.recover_igir_stuck()
    if not result.get("success"):
        raise HTTPException(status_code=429, detail=result.get("message", "Recovery failed"))
    return result


@router.post("/igir/jobs/cancel-all")
async def cancel_all_igir_jobs(request: Request):
    """Cancel all queued and processing igir jobs."""
    confirmation = request.headers.get("x-chd-action-confirm", "")
    if confirmation != "cancel-all-igir-jobs":
        raise HTTPException(
            status_code=400,
            detail="Missing confirmation header for cancel-all-igir-jobs action",
        )

    result = await job_manager.cancel_all_igir_jobs()
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Cancel all igir jobs requested from %s; queued=%d processing=%d requested=%d",
        client_host,
        result.get("queued", 0),
        result.get("processing", 0),
        result.get("requested", 0),
    )
    return result


@router.get("/igir/jobs/{job_id}", response_model=IgirJob)
async def get_igir_job(job_id: str):
    """Get a specific igir job by ID."""
    job = job_manager.get_igir_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Igir job not found")
    return job


@router.delete("/igir/jobs/{job_id}")
async def cancel_igir_job(job_id: str):
    """Cancel or delete an igir job."""
    job = job_manager.get_igir_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Igir job not found")

    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        await job_manager.delete_igir_job(job_id)
        return {"status": "deleted"}

    if await job_manager.cancel_igir_job(job_id):
        return {"status": "cancelled"}

    raise HTTPException(status_code=400, detail="Cannot cancel igir job")


@router.get("/igir/jobs/{job_id}/log")
async def get_igir_job_log(job_id: str):
    """Get the full output log for a completed igir job."""
    job = job_manager.get_igir_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Igir job not found")

    log = job_manager.get_igir_job_log(job_id)
    return {
        "job_id": job_id,
        "status": job.status.value,
        "command_preview": job.command_preview,
        "lines": log or [],
        "line_count": len(log) if log else 0,
    }


@router.get("/igir/jobs/{job_id}/events")
async def igir_job_progress(job_id: str):
    """SSE endpoint for a specific igir job's progress."""
    job = job_manager.get_igir_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Igir job not found")

    async def event_generator():
        queue = None
        try:
            # Send initial state
            yield {
                "event": "status",
                "data": json.dumps({
                    "job_id": job_id,
                    "status": job.status.value,
                    "progress": job.progress,
                    "phase": job.phase,
                    "message": job.message,
                }),
            }

            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return

            queue = job_manager.subscribe_igir(job_id)
            latest_job = job_manager.get_igir_job(job_id)
            if latest_job and latest_job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                yield {
                    "event": "status",
                    "data": json.dumps({
                        "job_id": job_id,
                        "status": latest_job.status.value,
                        "progress": latest_job.progress,
                        "phase": latest_job.phase,
                        "message": latest_job.message,
                    }),
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
                    yield {"event": "ping", "data": json.dumps({})}

        except Exception as e:
            logger.error("SSE igir job progress error: %s", e)
        finally:
            if queue:
                job_manager.unsubscribe_igir(job_id, queue)

    return EventSourceResponse(event_generator())


# ──────────────────────── Validation & Preview ────────────────────────


@router.post("/igir/validate", response_model=IgirValidationResult)
async def validate_igir_request(request: IgirJobCreateRequest):
    """Validate an igir job request and return errors, warnings, and command preview."""
    return igir_service.validate_request(request)


@router.post("/igir/preflight")
async def igir_preflight(request: IgirJobCreateRequest):
    """Run validation + filesystem safety checks before creating an igir job."""
    validation = igir_service.validate_request(request)
    errors = list(validation.errors)
    warnings = list(validation.warnings)
    path_checks: dict[str, object] = {
        "inputs": [],
        "output": None,
        "dats": [],
        "patches": [],
    }
    risk_factors: list[str] = []

    commands = set(request.commands or [])
    command_values = {c.value for c in commands}
    has_destructive = False
    if "move" in command_values:
        has_destructive = True
        risk_factors.append("move command modifies source inputs")
    if "clean" in command_values and not request.clean_dry_run:
        has_destructive = True
        risk_factors.append("clean command deletes unknown files")
    if request.overwrite or request.overwrite_invalid:
        has_destructive = True
        risk_factors.append("overwrite is enabled")

    # Input checks
    for input_path in request.input_paths or []:
        glob_like = _is_glob_like(input_path)
        exists = glob_like or os.path.exists(input_path)
        readable = glob_like or os.access(input_path, os.R_OK)
        item = {
            "path": input_path,
            "glob": glob_like,
            "exists": exists,
            "readable": readable,
        }
        path_checks["inputs"].append(item)
        if not exists and not glob_like:
            warnings.append(f"Input path does not exist: {input_path}")
        if exists and not readable:
            errors.append(f"Input path is not readable: {input_path}")

    # DAT/Patch checks (best effort)
    for dat_path in request.dat_paths or []:
        exists = os.path.exists(dat_path)
        path_checks["dats"].append({"path": dat_path, "exists": exists})
        if not exists and not _is_glob_like(dat_path):
            warnings.append(f"DAT path does not exist: {dat_path}")

    for patch_path in request.patch or []:
        exists = os.path.exists(patch_path)
        path_checks["patches"].append({"path": patch_path, "exists": exists})
        if not exists and not _is_glob_like(patch_path):
            warnings.append(f"Patch path does not exist: {patch_path}")

    # Output checks
    output_disk_free_bytes = None
    if request.output_path:
        output_exists = os.path.exists(request.output_path)
        output_is_dir = output_exists and os.path.isdir(request.output_path)
        writable_probe = request.output_path if output_exists else str(Path(request.output_path).parent)
        writable_target_path = _nearest_existing_parent(writable_probe)
        writable_target = str(writable_target_path)
        writable = os.access(writable_target, os.W_OK)
        path_checks["output"] = {
            "path": request.output_path,
            "exists": output_exists,
            "is_directory": output_is_dir,
            "writable": writable,
            "writable_target": writable_target,
        }
        if output_exists and not output_is_dir:
            errors.append(f"Output path is not a directory: {request.output_path}")
        if not writable:
            errors.append(f"Output path is not writable: {request.output_path}")
        if not output_exists and not Path(request.output_path).parent.exists():
            warnings.append(
                "Output parent directory does not exist yet; "
                "it will be created at runtime if permissions allow",
            )

        try:
            output_disk_free_bytes = shutil.disk_usage(writable_target).free
        except OSError:
            output_disk_free_bytes = None

        for input_path in request.input_paths or []:
            if _is_glob_like(input_path):
                continue
            if _path_overlaps(request.output_path, input_path):
                warnings.append(
                    f"Output path overlaps input path: {request.output_path} <> {input_path}",
                )
                if "clean" in command_values:
                    has_destructive = True
                    risk_factors.append("clean with overlapping input/output paths")

        if (
            "clean" in command_values
            and request.output_path not in (request.input_paths or [])
        ):
            warnings.append(
                "clean is enabled but output path is not explicitly included as an input path; "
                "this can remove expected files in merge workflows",
            )

    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    risk_factors = list(dict.fromkeys(risk_factors))

    return {
        "valid": len(errors) == 0 and validation.valid,
        "errors": errors,
        "warnings": warnings,
        "command_preview": validation.command_preview,
        "requires_confirmation": has_destructive,
        "risk_factors": risk_factors,
        "path_checks": path_checks,
        "output_disk_free_bytes": output_disk_free_bytes,
    }


@router.post("/igir/dry-run", response_model=IgirValidationResult)
async def igir_dry_run(request: IgirJobCreateRequest):
    """Validate and preview an igir job with --clean-dry-run enabled.

    Overrides clean_dry_run=True on the request so the user can preview
    which files would be cleaned without executing the full job.
    """
    # Force clean_dry_run on for preview
    request.clean_dry_run = True

    # Validate first
    validation = igir_service.validate_request(request)
    if not validation.valid:
        return validation

    # Return validation with dry-run note
    validation.warnings.append(
        "Dry-run mode: clean operations will only preview, not delete files",
    )
    return validation


@router.post("/igir/dry-run/execute")
async def igir_dry_run_execute(request: IgirJobCreateRequest):
    """Execute a safe clean dry-run preview and return candidate files."""
    preview_request = request.model_copy(deep=True)
    preview_request.commands = [IgirCommand.CLEAN]
    preview_request.clean_dry_run = True
    # Prevent side effects during preview runs. IgirService.run() ensures
    # output_path exists before launch, so unset it for clean dry-run execute.
    preview_request.output_path = None

    validation = igir_service.validate_request(preview_request)
    if not validation.valid:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid igir dry-run request",
                "errors": validation.errors,
                "warnings": validation.warnings,
                "command_preview": validation.command_preview,
            },
        )

    clean_lines: list[str] = []
    latest_phase = ""
    latest_message = ""
    try:
        async for update in job_manager.run_igir_preview(
            preview_request,
            cancel_event=asyncio.Event(),
        ):
            latest_phase = str(update.get("phase", latest_phase))
            latest_message = str(update.get("message", latest_message))
            if update.get("clean_dry_run_results"):
                clean_lines = list(update.get("clean_dry_run_results") or [])
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc

    return {
        "valid": True,
        "command_preview": validation.command_preview,
        "warnings": validation.warnings + [
            "Dry-run execute mode forced command set to clean only",
        ],
        "phase": latest_phase,
        "message": latest_message,
        "clean_dry_run_results": clean_lines,
        "count": len(clean_lines),
    }


@router.post("/igir/quick-setup")
async def igir_quick_setup(request: IgirQuickSetupRequest):
    """Recommend a practical igir starter configuration for selected inputs."""
    if not request.input_paths:
        raise HTTPException(status_code=400, detail="At least one input path is required")

    valid_inputs: list[str] = []
    for input_path in request.input_paths:
        if not is_within_configured_volumes(input_path, treat_archives=False):
            continue
        if _is_glob_like(input_path):
            valid_inputs.append(input_path)
            continue

        resolved = os.path.realpath(input_path)
        if not (os.path.isdir(resolved) or os.path.isfile(resolved)):
            continue
        valid_inputs.append(resolved)

    if not valid_inputs:
        raise HTTPException(
            status_code=400,
            detail="No valid input paths found inside configured volumes",
        )

    dat_entries = await igir_service.search_dats()
    input_tokens = _build_auto_setup_tokens(valid_inputs)
    recommended_dats = _recommend_dat_paths(dat_entries, input_tokens)
    output_path = _recommend_output_path(valid_inputs[0])
    workflows = _build_workflow_templates(output_path, recommended_dats)
    selected = _select_workflow(workflows, request.goal)
    requires_dats = bool(selected.get("requires_dats"))
    has_dats = len(recommended_dats) > 0

    warning = None
    if requires_dats and not has_dats:
        warning = (
            f"Workflow '{selected.get('label')}' benefits from DAT files, "
            "but no matching DATs were auto-detected."
        )

    return {
        **selected,
        "filter_preset": "retail",
        "input_count": len(valid_inputs),
        "matched_tokens": sorted(input_tokens),
        "workflow_id": selected.get("id"),
        "workflow_label": selected.get("label"),
        "workflows": workflows,
        "warning": warning,
    }


@router.post("/igir/feature-events")
async def track_igir_feature_event(request: IgirFeatureEventRequest):
    """Track lightweight igir feature-adoption events."""
    event = (request.event or "").strip().lower()
    if event not in _SUPPORTED_FEATURE_EVENTS:
        raise HTTPException(status_code=400, detail="Unsupported feature event")

    total = _record_feature_event(event, request.value)
    logger.info("igir feature event tracked event=%s total=%d", event, total)
    return {"event": event, "total": total}


@router.get("/igir/feature-events")
async def list_igir_feature_events():
    """Return in-memory counts for igir feature-adoption events."""
    with _feature_event_lock:
        snapshot = dict(_feature_event_counts)
    return {"events": snapshot}


# ──────────────────────── DAT Management ────────────────────────


@router.get("/igir/dats")
async def list_dats(
    path: str | None = Query(
        default=None,
        description="Subdirectory within the DAT root to list",
    ),
):
    """List DAT files and subdirectories at the given path.

    If no path is given, lists the root DAT directory.
    """
    try:
        listing = await igir_service.list_dats(subdir=path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return listing.model_dump(mode="json")


@router.get("/igir/dats/search")
async def search_dats():
    """Recursively search for all DAT files in the DAT directory."""
    results = await igir_service.search_dats()
    return [entry.model_dump(mode="json") for entry in results]


# ──────────────────────── Tool Info ────────────────────────


@router.get("/igir/version")
async def get_igir_version():
    """Get the installed igir version."""
    version = await igir_service.get_version()
    return {"version": version, "path": settings.igir_path}
