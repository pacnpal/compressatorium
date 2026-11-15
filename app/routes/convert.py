import asyncio
import functools
from logging_setup import get_logger
import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Optional

from config import settings
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from models import (
    AutoBatchResponse,
    AutoJobCreateRequest,
    AutoPlanItem,
    AutoPlanRequest,
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
from routes.preferences import CONVERSION_KEY as CONVERSION_PREFS_KEY
from services.archive import archive_service
from services.job_manager import (
    ROLLBACK_WAIT_CAP_SECONDS,
    VOLUME_PROBE_TIMEOUT_SECONDS,
    DestinationUnresolvableError,
    OutputClaimedError,
    QueueBackpressureError,
    job_manager,
)
from services import referrers
from services.platform_detect import plan_auto
from services.preferences_store import preferences_store
from services.romz import romz_service
from services.subprocess_runner import run_detached
from services.output_conflicts import (
    OutputPathExhausted,
    OutputPathLocked,
    check_output_conflicts,
    input_priority,
)
from services.output_conflicts import (
    get_unique_output_path as _get_unique_output_path,
)
from services.tools import InputKind, ModeKind, registry
from sse_starlette.sse import EventSourceResponse
from utils.delete_plan import build_delete_plan, build_delete_snapshot
from utils.path_utils import (
    is_safe_directory_tree,
    is_within_configured_volumes,
    match_extension,
    source_companions_are_safe,
    strip_archive_path,
)

router = APIRouter()
logger = get_logger()


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


# Confirmation tokens for the destructive queue actions. The frontend mirrors
# these in src/lib/api/client.js (CONFIRM); keeping one backend definition each
# means a rename can't silently break the X-CHD-Action-Confirm guard.
ACTION_CONFIRM_HEADER = "x-chd-action-confirm"
CONFIRM_CANCEL_ALL_JOBS = "cancel-all-jobs"
CONFIRM_CLEAR_COMPLETED_JOBS = "clear-completed-jobs"

# Which modes support delete-on-verify is a registry fact
# (spec.supports_delete_on_verify); this is the single human-readable
# enumeration reused by every "unsupported" 400 (single create, batch, delete-plan).
_DELETE_ON_VERIFY_UNSUPPORTED_DETAIL = (
    "Delete-on-verify is only supported for "
    "create/copy/Dolphin/3DS/Switch-compress/CSO/CSO2/ZSO/DAX-compress modes"
)


def _validate_request_compression(
    spec, mode: str, compression: str | None, delete_on_verify: bool = False,
) -> None:
    """Reject a compression request a mode can't honor (shared by single + batch).

    Raises ``HTTPException(400)`` with the mode-specific message; a no-op when no
    compression was requested or the mode accepts it.
    """
    # Delete-on-verify removes the source once verify() passes, which assumes
    # verify() is a real content check. For most tools it is; jwud's is a
    # structural WUX walk backed by JWUDTool's byte-for-byte comparison during
    # the conversion, and the job can turn that comparison off. The plugin
    # decides (default True), so this is a registry lookup, not a tool branch.
    if delete_on_verify and not registry.for_mode(mode).delete_on_verify_is_safe(
        mode, compression,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Delete sources after verification cannot be combined with "
                "skipping verification: nothing would compare the output against "
                "the source before the source is deleted"
            ),
        )

    if not compression:
        return
    if spec.tool_id == "chdman" and spec.kind == ModeKind.EXTRACT:
        raise HTTPException(
            status_code=400,
            detail="Compression is only supported for CHD creation/copy",
        )
    if ":" in compression and not spec.supports_compression_level:
        raise HTTPException(
            status_code=400,
            detail="Compression levels are only supported for Dolphin and Switch formats",
        )
    if (
        spec.tool_id == "dolphin"
        and not (spec.supports_compression or spec.supports_compression_level)
    ):
        raise HTTPException(
            status_code=400,
            detail=_NO_COMPRESSION_DETAIL.get(
                mode, "Compression is not supported for this Dolphin mode"
            ),
        )
    if spec.tool_id == "dolphin" and "," in compression:
        raise HTTPException(
            status_code=400,
            detail="Dolphin compression supports only one codec at a time",
        )


def _validate_delete_on_verify(spec, delete_on_verify: bool) -> None:
    """Reject delete-on-verify on a mode that doesn't support it (single + batch)."""
    if delete_on_verify and not spec.supports_delete_on_verify:
        raise HTTPException(
            status_code=400,
            detail=_DELETE_ON_VERIFY_UNSUPPORTED_DETAIL,
        )


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


def get_unique_output_path(base_path: str, mode: str | None = None) -> str:
    """API-layer wrapper over the shared unique-output-path probe.

    The probe itself lives in ``services.output_conflicts`` so the RomM
    automation sweep resolves duplicates exactly the way ``/api/jobs`` does.
    Only the failure *shape* is a route concern: a rename into a locked
    directory subtree becomes ``SkipFile(OUTPUT_LOCKED)`` here, so the job
    pipeline defers and requeues it like any other locked output.

    Why the service refuses rather than probing on: every numbered sibling it
    would try shares ``base_path``'s parent and so falls inside the same held
    subtree, which would spin the loop with no sleep until the dir lock
    released. Only a *directory* lock triggers this; an ordinary single-file
    lock leaves the numbered siblings free and still probes normally.
    """
    try:
        return _get_unique_output_path(base_path, mode)
    except (OutputPathLocked, OutputPathExhausted):
        raise SkipFile(SkipReason.OUTPUT_LOCKED) from None


def _input_extension(path: str) -> str:
    if "::" in path:
        _, internal = path.split("::", 1)
        return Path(internal).suffix.lower()
    return Path(path).suffix.lower()


def _declares_input(path: str, spec) -> bool:
    """Whether ``spec`` declares an input extension matching ``path``.

    Archive-aware (an ``archive.zip::game.nsp`` member is judged on the
    member's name, not the ``.zip`` container) and suffix-based via the shared
    ``match_extension``, so a mode declaring a compound extension
    (nkit2iso's ``.nkit.iso``) validates the same way as a plain one.
    """
    name = path.split("::", 1)[1] if "::" in path else path
    return match_extension(name, spec.input_extensions) is not None


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
    NSZ_BAD_EXTENSION = "nsz_bad_extension"
    CSO_BAD_EXTENSION = "cso_bad_extension"
    ROMZ_BAD_EXTENSION = "romz_bad_extension"
    ROMZ_INVALID_ARCHIVE = "romz_invalid_archive"
    NKIT_BAD_EXTENSION = "nkit_bad_extension"
    JWUD_BAD_EXTENSION = "jwud_bad_extension"
    XISO_BAD_EXTENSION = "xiso_bad_extension"
    XISO_NOT_XBOX_IMAGE = "xiso_not_xbox_image"
    SOURCE_NOT_INDEPENDENTLY_CONVERTIBLE = "source_not_independently_convertible"
    DOLPHIN_SAME_PATH = "dolphin_same_path"
    CHAIN_BAD_EXTENSION = "chain_bad_extension"
    PS3_FOLDER_INVALID = "ps3_folder_invalid"
    PS3_OUTPUT_INSIDE_SOURCE = "ps3_output_inside_source"
    PS3_OUTPUT_OUTSIDE_VOLUMES = "ps3_output_outside_volumes"
    PS3_FOLDER_UNSAFE = "ps3_folder_unsafe"
    SOURCE_COMPANION_UNSAFE = "source_companion_unsafe"


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
        "Archive inputs are not supported for CHDMAN extract/copy modes "
        "(the source must be a finished .chd on disk)",
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
        "z3ds_compress requires Nintendo 3DS ROMs (.cci, .cia, .3ds, .cxi, .3dsx); "
        "z3ds_decompress requires compressed 3DS files "
        "(.zcci, .zcia, .z3ds, .zcxi, .z3dsx)",
    ),
    SkipReason.NSZ_BAD_EXTENSION: (
        400,
        "nsz_compress requires .nsp/.xci; nsz_decompress requires .nsz/.xcz",
    ),
    SkipReason.CSO_BAD_EXTENSION: (
        400,
        "cso_compress/cso2_compress/zso_compress/dax_compress require .iso; "
        "cso_decompress requires .cso/.zso/.dax",
    ),
    SkipReason.ROMZ_BAD_EXTENSION: (
        400,
        "romz_7z/romz_zip require .gb/.gbc/.gba/.nds; "
        "romz_extract requires .7z/.zip",
    ),
    SkipReason.ROMZ_INVALID_ARCHIVE: (
        422,
        "Archive is not a single handheld-ROM archive produced by this tool "
        "(corrupt, multi-file, or holds no ROM)",
    ),
    SkipReason.NKIT_BAD_EXTENSION: (
        400,
        "nkit_restore requires an NKit-shrunk GameCube/Wii image "
        "(.nkit.iso, .nkit.gcz)",
    ),
    SkipReason.JWUD_BAD_EXTENSION: (
        400,
        "jwud_compress requires a Wii U disc image (.wud); "
        "jwud_decompress requires a compressed image (.wux)",
    ),
    SkipReason.XISO_BAD_EXTENSION: (
        400,
        "xiso_rewrite requires an Xbox / Xbox 360 disc image (.iso)",
    ),
    SkipReason.XISO_NOT_XBOX_IMAGE: (
        400,
        "File is not an untrimmed Xbox / Xbox 360 disc image (no XDVDFS volume "
        "was found, or it is already an optimized XISO)",
    ),
    SkipReason.SOURCE_NOT_INDEPENDENTLY_CONVERTIBLE: (
        400,
        "File is one part of a multi-file source and cannot be converted on its "
        "own; select the set's primary file instead (for a split Wii U dump, "
        "that is game_part1.wud)",
    ),
    SkipReason.DOLPHIN_SAME_PATH: (
        400,
        "Output path matches input; overwriting would delete the source file",
    ),
    SkipReason.CHAIN_BAD_EXTENSION: (
        400,
        "cso_to_chd requires a .cso/.zso/.dax source; "
        "nkit_to_rvz requires an NKit image (.nkit.iso, .nkit.gcz)",
    ),
    SkipReason.PS3_FOLDER_INVALID: (
        400,
        "folder_to_iso requires a decrypted PS3 disc/JB folder "
        "(a PS3_GAME/ root, plus PS3_DISC.SFB for disc rips)",
    ),
    SkipReason.PS3_OUTPUT_INSIDE_SOURCE: (
        400,
        "Output .iso would be written inside the source folder being packed; "
        "choose an output directory outside the PS3 folder",
    ),
    SkipReason.PS3_OUTPUT_OUTSIDE_VOLUMES: (
        400,
        "Default output .iso would land outside the configured volumes "
        "(the PS3 folder is a volume root); choose an in-volume output directory",
    ),
    SkipReason.PS3_FOLDER_UNSAFE: (
        400,
        "PS3 folder contains symlinks or non-regular entries and cannot be packed safely",
    ),
    SkipReason.SOURCE_COMPANION_UNSAFE: (
        400,
        "A companion file this source consumes is a symlink or resolves outside "
        "the configured volumes (for a split Wii U dump, one of the other "
        "game_partN.wud files)",
    ),
}


# Tools whose plan-time input validation is the generic "the input extension
# must be one the mode declares" check (collapsed from the former per-tool
# is_<tool> ladder, design §3.1). chdman is intentionally absent: it validates
# by .chd presence (create needs non-.chd, extract/copy need .chd) because it
# drops .chd from input_extensions. Each tool keeps its own skip reason/message.
_BAD_EXTENSION_REASON: dict[str, SkipReason] = {
    "dolphin": SkipReason.DOLPHIN_BAD_EXTENSION,
    "z3ds": SkipReason.Z3DS_BAD_EXTENSION,
    "nsz": SkipReason.NSZ_BAD_EXTENSION,
    "cso": SkipReason.CSO_BAD_EXTENSION,
    "chain": SkipReason.CHAIN_BAD_EXTENSION,
    "romz": SkipReason.ROMZ_BAD_EXTENSION,
    "nkit": SkipReason.NKIT_BAD_EXTENSION,
    "jwud": SkipReason.JWUD_BAD_EXTENSION,
    "xiso": SkipReason.XISO_BAD_EXTENSION,
}

# Why a file a tool claims by extension was still rejected by its per-file
# ``converts_path`` refinement. The default describes the multi-file-source
# case (jwud's split Wii U dumps); a tool that refines on *content* instead
# names its own reason, so the user is told what is actually wrong.
_NOT_CONVERTIBLE_REASON: dict[str, SkipReason] = {
    "xiso": SkipReason.XISO_NOT_XBOX_IMAGE,
}


# Dolphin modes that take no compression input, with their specific advisory
# message. Collapsed from the former per-mode dolphin_iso / dolphin_gcz branches
# via a data lookup (not a capability branch), so the exact wire detail is
# preserved while the gate is driven by spec fields. Scoped to dolphin because
# other tools that merely ignore compression (e.g. the cso_to_chd chain, whose
# ChainTool drops a stale preset) historically queued rather than 400'd.
_NO_COMPRESSION_DETAIL: dict[str, str] = {
    "dolphin_iso": "Compression not applicable for ISO extraction",
    "dolphin_gcz": "GCZ uses fixed internal compression",
}


async def _plan_directory_job(
    file_path: str,
    *,
    mode: str,
    output_dir: str | None,
    duplicate_action: DuplicateAction,
) -> JobPlan:
    """Resolve a directory-input job (makeps3iso folder->iso) into a ``JobPlan``.

    Delete-on-verify is not offered for directory modes (the tool's
    ``supports_delete_on_verify`` is False and the route blocks it upstream), so
    there is no delete snapshot here.
    """
    if not await run_in_threadpool(os.path.isdir, file_path):
        raise SkipFile(SkipReason.FILE_NOT_FOUND)
    # Registry-driven: the tool's accepts_directory runs its source-layout
    # detector (PS3 disc/JB layout) off the event loop — no tool-identity branch.
    accepts = await run_in_threadpool(
        registry.for_mode(mode).accepts_directory, file_path,
    )
    if not accepts:
        raise SkipFile(SkipReason.PS3_FOLDER_INVALID)

    # Canonicalize the source to its real, symlink-free path before deriving the
    # job, and pack *that* path. makeps3iso reads the source tree as a native
    # subprocess, so a submitted path with a symlink in an ancestor component
    # (e.g. "/vol/link/MyGame" with "link" -> "/vol/real") would otherwise let a
    # concurrent swap of that link retarget the native reader to an unchecked
    # tree after validation. Resolving to the real path removes that mutable
    # window; a symlinked *root* is still rejected outright by
    # `is_safe_directory_tree` below (which runs on the original submitted path).
    source_real = await run_in_threadpool(os.path.realpath, file_path)

    normalized = os.path.normpath(source_real)
    display_filename = os.path.basename(normalized)
    output_path = await run_in_threadpool(
        _get_output_path, mode, normalized, output_dir,
    )
    base_output_path = output_path

    # Refuse to write the ISO inside the very folder being packed: makeps3iso
    # walks the source tree, so an output under it would be (partially) ingested
    # into itself and corrupt the image. The default output is a sibling
    # ("<folder>.iso"), so this only triggers when output_dir is set to the
    # source folder or a descendant. (The lock manager's subtree protection
    # guards against *other* jobs, not a job's own output.) Resolve symlinks
    # first so a symlinked output dir into the tree can't slip past this.
    output_real = await run_in_threadpool(os.path.realpath, output_path)
    if output_real == source_real or output_real.startswith(source_real + os.sep):
        raise SkipFile(SkipReason.PS3_OUTPUT_INSIDE_SOURCE)

    # The route only validates a user-supplied output_dir; the *derived* sibling
    # output is unchecked. When the PS3 folder is itself a volume root, that
    # sibling lands outside all configured volumes, so enforce the same volume
    # boundary the rest of the API treats as the access edge.
    if not await run_in_threadpool(
        is_within_configured_volumes, output_path, treat_archives=False,
    ):
        raise SkipFile(SkipReason.PS3_OUTPUT_OUTSIDE_VOLUMES)

    # check_output_conflicts is split-set aware for folder_to_iso (its
    # companion_outputs scans for numbered parts), so a prior split build counts
    # as an existing output here and in the /jobs/check-duplicates preflight
    # alike. Off the event loop because that companion lookup hits the disk.
    output_exists, is_locked = await run_in_threadpool(
        check_output_conflicts, mode, output_path,
    )
    if output_exists or is_locked:
        if duplicate_action == DuplicateAction.SKIP:
            raise SkipFile(
                SkipReason.OUTPUT_LOCKED if is_locked else SkipReason.OUTPUT_EXISTS,
            )
        if duplicate_action == DuplicateAction.OVERWRITE:
            if is_locked:
                raise SkipFile(SkipReason.OUTPUT_LOCKED)
        elif duplicate_action == DuplicateAction.RENAME:
            # Companion-aware (steps past a split set's numbered parts); off the
            # event loop because folder_to_iso's companion lookup scans the dir.
            output_path = await run_in_threadpool(
                get_unique_output_path, output_path, mode,
            )

    # Recursive PS3 safety walk, deferred until the job is known to actually run.
    # Skip/locked collisions above short-circuit first, so a batch SKIP over an
    # already-converted large PS3 library isn't forced to stat every entry only
    # to skip with OUTPUT_EXISTS. Jobs that will queue are still validated here,
    # and `_process_job` revalidates the exact tree again after acquiring the
    # directory lock, immediately before makeps3iso runs.
    if not await run_in_threadpool(is_safe_directory_tree, file_path):
        raise SkipFile(SkipReason.PS3_FOLDER_UNSAFE)

    allow_overwrite = (
        duplicate_action == DuplicateAction.OVERWRITE and output_exists
    )
    return JobPlan(
        file_path=source_real,
        output_path=output_path,
        base_output_path=base_output_path,
        allow_overwrite=allow_overwrite,
        display_filename=display_filename,
        delete_snapshot=None,
        priority=0,
    )


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
    # Directory-as-input mode (makeps3iso folder->iso): a folder, not a file with
    # a suffix. Validate isdir + the tool's source-layout detector instead of
    # isfile + an extension match, and derive the output / display name from the
    # NORMALIZED basename (a trailing slash otherwise yields "" -> ".iso").
    if InputKind.DIRECTORY in spec.input_kinds:
        return await _plan_directory_job(
            file_path,
            mode=mode,
            output_dir=output_dir,
            duplicate_action=duplicate_action,
        )

    archive_source_dir = None  # Directory where the archive is located (for output)
    output_path = None
    base_output_path = None
    output_exists = False

    display_filename = None
    bad_ext_reason = _BAD_EXTENSION_REASON.get(spec.tool_id)

    # Handle archive files
    if "::" in file_path:
        if not spec.allows_archive_input:
            raise SkipFile(SkipReason.ARCHIVE_INPUT_NOT_ALLOWED)
        archive_path, internal_path = file_path.split("::", 1)
        archive_source_dir = os.path.dirname(archive_path)  # Save CHD next to archive
        display_filename = os.path.basename(internal_path)

        if not os.path.isfile(archive_path):
            raise SkipFile(SkipReason.ARCHIVE_NOT_FOUND)

        if bad_ext_reason is not None and not _declares_input(file_path, spec):
            raise SkipFile(bad_ext_reason)

        # Calculate output path before extraction to avoid unnecessary work.
        # Use the extension-preserving flattened name so tools whose output
        # extension is derived from the input (e.g. z3ds .3ds -> .z3ds) map
        # correctly; chdman/dolphin strip it back off via Path.stem.
        effective_output_dir = output_dir or archive_source_dir
        output_name = archive_service._output_name_for_member(internal_path)
        # Off the event loop: output_path() is pure string work for most tools,
        # but romz extract peeks inside the archive to derive the ROM's name.
        output_path = await run_in_threadpool(
            _get_output_path, mode, output_name, effective_output_dir,
            treat_as_stem=True,
        )
        base_output_path = output_path

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if duplicate_action == DuplicateAction.SKIP:
                raise SkipFile(
                    SkipReason.OUTPUT_LOCKED if is_locked else SkipReason.OUTPUT_EXISTS,
                )
            if duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise SkipFile(SkipReason.OUTPUT_LOCKED)
            elif duplicate_action == DuplicateAction.RENAME:
                output_path = _renamed_output(mode, output_path, duplicate_action)

    if "::" not in file_path and not os.path.isfile(file_path):
        raise SkipFile(SkipReason.FILE_NOT_FOUND)

    if (
        spec.tool_id == "chdman" and spec.kind in (ModeKind.EXTRACT, ModeKind.COPY)
    ) and not file_path.lower().endswith(".chd"):
        raise SkipFile(SkipReason.EXTRACT_COPY_REQUIRES_CHD)

    if spec.kind == ModeKind.CREATE and file_path.lower().endswith(".chd"):
        raise SkipFile(SkipReason.CREATE_REQUIRES_NON_CHD)

    # Generic input-extension gate (collapsed from the per-tool is_<tool>
    # ladder, design §3.1): every non-chdman tool validates that the
    # archive-aware input extension (the member's, not the ".zip" container's)
    # is one its mode declares. spec.input_extensions is per-mode, so each
    # direction is validated against the right set. chdman is handled above by
    # the .chd create/extract checks (it drops .chd from input_extensions). The
    # per-tool skip reason carries the tool-specific message.
    if bad_ext_reason is not None:
        if not _declares_input(file_path, spec):
            raise SkipFile(bad_ext_reason)

        # Per-file refinement of the same gate: `converts_path` defaults to the
        # very extension match above, so this is a no-op for every tool whose
        # source is one file. A tool whose source is a *set* (jwud's split Wii U
        # dumps) accepts only the primary member, so the others are rejected
        # here rather than queued into a job that can only fail. A tool can also
        # refine on *content* — xiso only accepts an ``.iso`` that really holds
        # an untrimmed Xbox filesystem — and `_NOT_CONVERTIBLE_REASON` lets it
        # say so instead of borrowing jwud's multi-file message. Scoped to the
        # tools that opted into the generic gate: chdman drops `.chd` from its
        # `input_extensions` and validates by `.chd` presence above, so the
        # default extension-match refinement does not describe it. Runs off the
        # event loop — the probe stats the sibling primary.
        if not await run_in_threadpool(
            registry.for_mode(mode).converts_path, file_path,
        ):
            raise SkipFile(_NOT_CONVERTIBLE_REASON.get(
                spec.tool_id, SkipReason.SOURCE_NOT_INDEPENDENTLY_CONVERTIBLE,
            ))

    # A multi-file source drags in siblings the request never named, and the
    # converter opens them itself — JNUSLib enumerates a split set's parts from
    # part 1 rather than taking a list from us. So the volume boundary the route
    # enforced on `file_path` has to be enforced on the companions too, or a
    # planted `game_part2.wud` symlink would have the tool read a file outside
    # the configured volumes and fold its bytes into the output. Registry-driven
    # and a no-op for the eight single-file tools (`source_companions` is `[]`).
    # Off the event loop: it stats and resolves each companion.
    if not await run_in_threadpool(
        source_companions_are_safe, file_path, mode,
    ):
        raise SkipFile(SkipReason.SOURCE_COMPANION_UNSAFE)

    if mode == "romz_extract":
        # romz-specific: validate the archive is a real single-ROM archive
        # BEFORE planning an output / allowing overwrite. get_output_path_for_mode
        # falls back to the suffix-stripped stem for unreadable/invalid archives,
        # so without this an ordinary/corrupt/multi-ROM archive next to an
        # existing same-stem file could drive duplicate_action=overwrite to
        # delete that unrelated file before convert() ever validates.
        try:
            await run_in_threadpool(
                romz_service._single_rom_member, file_path,
            )
        except Exception as exc:
            # Broad by design: corrupt/multi-ROM archives surface as
            # ValueError, zipfile.BadZipFile, py7zr errors, OSError, … and
            # all map to the same skip. Log the cause for troubleshooting.
            logger.debug(
                "romz_extract validation failed for %s: %s", file_path, exc,
            )
            raise SkipFile(SkipReason.ROMZ_INVALID_ARCHIVE) from None

    # Calculate output path and handle duplicates
    # For archive files: use output_dir if specified, otherwise save next to archive
    if output_path is None:
        effective_output_dir = output_dir or archive_source_dir
        # Off the event loop: romz extract reads the archive to name its output.
        output_path = await run_in_threadpool(
            _get_output_path, mode, file_path, effective_output_dir,
        )
        base_output_path = output_path

        output_exists, is_locked = check_output_conflicts(mode, output_path)
        if output_exists or is_locked:
            if duplicate_action == DuplicateAction.SKIP:
                raise SkipFile(
                    SkipReason.OUTPUT_LOCKED if is_locked else SkipReason.OUTPUT_EXISTS,
                )
            if duplicate_action == DuplicateAction.OVERWRITE:
                if is_locked:
                    raise SkipFile(SkipReason.OUTPUT_LOCKED)
            elif duplicate_action == DuplicateAction.RENAME:
                output_path = _renamed_output(mode, output_path, duplicate_action)

    if (
        spec.kind != ModeKind.COPY
        and duplicate_action == DuplicateAction.OVERWRITE
        and output_path
        and _is_same_path(output_path, file_path)
    ):
        # Any non-copy mode writing over its own source would destroy it; only
        # chdman copy (.chd -> .chd) is an intentional in-place recompress, and
        # every other mode changes the extension so output never equals input.
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
        priority=input_priority(file_path),
    )


async def _planned_output_path(mode: str, file_path: str, output_dir: str | None) -> str:
    """The output a conversion of *file_path* would be planned to write."""
    if "::" not in file_path:
        return await run_in_threadpool(_get_output_path, mode, file_path, output_dir)
    # An archive member is named from its internal filename and lands beside
    # the archive unless an output folder was chosen. Off the event loop: romz
    # extract reads the archive to name its output.
    archive_path, internal_path = file_path.split("::", 1)
    output_name = archive_service._output_name_for_member(internal_path)
    return await run_in_threadpool(
        _get_output_path, mode, output_name, output_dir or os.path.dirname(archive_path),
        treat_as_stem=True,
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

        output_path = await _planned_output_path(mode, file_path, output_dir)
        exists, _ = await run_in_threadpool(
            check_output_conflicts, mode, output_path,
        )

        results.append(
            DuplicateInfo(file_path=file_path, output_path=output_path, exists=exists),
        )

    return results


def _renamed_output(mode: str, output_path: str, duplicate_action: DuplicateAction) -> str:
    """The path a RENAME job writes when *output_path* is taken (or locked).

    Shared by ``plan_job`` and the delete-plan preview, so the confirmation
    names the same numbered output the job will write.
    """
    if duplicate_action != DuplicateAction.RENAME:
        return output_path
    exists, locked = check_output_conflicts(mode, output_path)
    return get_unique_output_path(output_path, mode) if exists or locked else output_path


async def _add_referrer_changes(
    item: dict, mode: str, file_path: str, output_dir: str | None,
    duplicate_action: DuplicateAction = DuplicateAction.SKIP,
) -> None:
    """List each Referrer's planned change on a delete-plan *item* (#295).

    The same renames the job hands the Referrer hook: every file the delete
    removes, mapped to the output the job will write (a RENAME's numbered
    path included).
    """
    item["referrer_changes"] = []
    delete_paths = item.get("delete_paths") or []
    if not delete_paths:
        return
    try:
        output_path = await run_in_threadpool(
            _renamed_output, mode,
            await _planned_output_path(mode, file_path, output_dir), duplicate_action,
        )
    except SkipFile as skip:
        # RENAME couldn't settle on an output name (no free numbered name, or
        # the output folder is busy right now). Record it as an error on this
        # item instead of a 500. Any item error sets the plan-wide `blocked`,
        # so this disables Confirm for the whole batch, and the message says so.
        logger.debug("delete-plan: no RENAME output for %s: %s", file_path, skip.reason)
        item.setdefault("errors", []).append(
            "Can't settle the output name a rename would use (no free numbered "
            "name, or the output folder is busy). This blocks the whole delete "
            "plan: retry, or leave this file out of the selection.",
        )
        return
    image_path = strip_archive_path(file_path) if "::" in file_path else file_path
    try:
        changes = await referrers.plan_changes_bounded(
            image_path, {p: output_path for p in delete_paths},
        )
    except Exception as exc:  # noqa: BLE001 - the job would keep the Source too
        target = getattr(exc, "target", None) or image_path
        reason = getattr(exc, "reason", None) or str(exc)
        item.setdefault("warnings", []).append(
            f"Couldn't update {os.path.basename(target)} ({reason}); "
            "the source will be kept",
        )
        return
    item["referrer_changes"] = [c.to_dict() for c in changes]


@router.post("/jobs/delete-plan")
async def delete_plan(request: DeletePlanRequest) -> dict:
    """Build a delete plan for delete-on-verify confirmation."""
    if not request.file_paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    mode = request.mode.value
    if not supports_delete_on_verify(mode):
        raise HTTPException(
            status_code=400,
            detail=_DELETE_ON_VERIFY_UNSUPPORTED_DETAIL,
        )

    output_dir = normalize_output_dir(request.output_dir)
    if output_dir and not is_within_configured_volumes(
        output_dir, treat_archives=False,
    ):
        raise HTTPException(
            status_code=403,
            detail="Access denied: output directory outside configured volumes",
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
            item = await run_in_threadpool(
                functools.partial(build_delete_plan, file_path, confine_to_volumes=True),
            )
            await _add_referrer_changes(
                item, mode, file_path, output_dir, request.duplicate_action,
            )

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
    _validate_request_compression(spec, mode, compression, request.delete_on_verify)
    _validate_delete_on_verify(spec, request.delete_on_verify)
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

    # Proactive queue-depth check: reject with 429 *before* planning if the
    # queue is already at capacity, so a full-queue submit doesn't pay for the
    # PS3 folder safety walk (which stats the whole source tree) just to be
    # rejected. Parity with the batch-create path, and surfaces backpressure
    # even when tests or callers stub out ``job_manager.create_job``.
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    if 0 < max_depth <= job_manager.get_queue_depth():
        raise HTTPException(
            status_code=429,
            detail=f"Conversion queue full ({max_depth} jobs). Retry later.",
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

    try:
        job = await job_manager.create_job(
            plan.file_path,
            request.mode,
            output_path=plan.output_path,
            allow_overwrite=plan.allow_overwrite,
            filename_override=plan.display_filename,
            compression=compression,
            delete_on_verify=request.delete_on_verify,
            container_only=request.container_only,
            split=request.split,
            delete_snapshot=plan.delete_snapshot,
        )
    except OutputClaimedError as exc:
        # A concurrency outcome, not a malformed request: another live job took
        # this destination between planning and queueing. 409 so the caller can
        # retry or skip; a 500 said "server broke" for something working
        # exactly as designed.
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except DestinationUnresolvableError as exc:
        # Transient and on the storage's side, not the caller's: the volume
        # holding the destination did not answer inside the probe bound, so the
        # collision check could not be made and nothing was queued. 503 says
        # "come back", which is the truth -- a 500 would read as a bug and a
        # 400 as a bad request.
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc

    return job


@dataclass
class PreparedBatch:
    """A batch planned to the last detail and not yet queued."""

    request: BatchJobCreateRequest
    compression: str | None
    plans: list[JobPlan]
    # file path -> why it was left out: a ``plan_job`` skip (the same words a
    # single-job request would get), or a file whose output another file of
    # the batch writes. Auto says so in its plan; the CLI reports each one
    # (#294, #306).
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def job_specs(self) -> list[dict]:
        return [
            {
                "file_path": plan.file_path,
                "output_path": plan.output_path,
                "allow_overwrite": plan.allow_overwrite,
                "filename_override": plan.display_filename,
                "delete_snapshot": plan.delete_snapshot,
            }
            for plan in self.plans
        ]


def _check_queue_room(adding: int) -> None:
    """429 when ``adding`` more jobs would push the queue past its depth."""
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    if max_depth > 0 and job_manager.get_queue_depth() + adding > max_depth:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Conversion queue would exceed capacity "
                f"({max_depth} jobs). Retry later."
            ),
        )


async def queue_prepared(batch: PreparedBatch) -> list[ConversionJob]:
    """Queue a prepared batch as one atomic job-manager call."""
    try:
        return await job_manager.create_jobs_atomic(
            batch.job_specs,
            batch.request.mode,
            compression=batch.compression,
            delete_on_verify=batch.request.delete_on_verify,
            container_only=batch.request.container_only,
            split=batch.request.split,
        )
    except OutputClaimedError as exc:
        # A concurrency outcome, not a malformed request: another live job took
        # this destination between planning and queueing. 409 so the caller can
        # retry or skip; a 500 said "server broke" for something working
        # exactly as designed.
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except DestinationUnresolvableError as exc:
        # Transient and on the storage's side, not the caller's: the volume
        # holding the destination did not answer inside the probe bound, so the
        # collision check could not be made and nothing was queued. 503 says
        # "come back", which is the truth -- a 500 would read as a bug and a
        # 400 as a bad request.
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    except QueueBackpressureError as exc:
        raise HTTPException(status_code=429, detail=exc.detail) from exc


@router.post("/jobs/batch", response_model=list[ConversionJob])
async def create_batch_jobs(request: BatchJobCreateRequest):
    """Create multiple conversion jobs."""
    batch = await prepare_batch(request)
    # Proactive batch backpressure: reject before enqueuing any jobs if
    # accepting the batch would push the queue past ``max_queue_depth``.
    # Keeps single-job and batch submission behaviour consistent.
    _check_queue_room(len(batch.plans))
    return await queue_prepared(batch)


def _check_within_volumes(file_paths, output_dir: str | None = None) -> None:
    """403 unless every path (and the output dir) is inside the Volumes.

    Blocking (``is_within_configured_volumes`` stats and resolves); run it
    through ``_off_loop_bounded``, never on the event loop.
    """
    for file_path in file_paths:
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


async def _off_loop_bounded(func, *args, **kwargs):
    """Run blocking Volume work off the event loop, bounded; 503 past the bound.

    On a disposable thread (``run_detached``), so a Volume that stops answering
    holds neither the loop nor a shared pool worker. Past
    ``VOLUME_PROBE_TIMEOUT_SECONDS`` the request answers 503 and the thread is
    abandoned; it may linger until the mount answers, and its result is
    dropped. A process-wide probe ceiling being full answers 503 the same way.
    """
    try:
        return await asyncio.wait_for(
            run_detached(func, *args, **kwargs), VOLUME_PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503, detail="A Volume isn't responding. Try again later.",
        ) from None


async def prepare_batch(request: BatchJobCreateRequest) -> PreparedBatch:
    """Validate and plan every file of a batch, queueing nothing.

    Raises the same HTTP errors ``create_batch_jobs`` always has. Split out so
    Auto can prepare every group before it queues any of them.
    """
    if request.mode in (ConversionMode.METADATA_SCAN, ConversionMode.DAT_MATCH):
        raise HTTPException(
            status_code=400,
            detail=f"{request.mode.value} is not a valid conversion mode",
        )
    compression = normalize_compression(request.compression)
    mode = request.mode.value
    spec = registry.spec(mode)
    output_dir = normalize_output_dir(request.output_dir)
    _validate_request_compression(spec, mode, compression, request.delete_on_verify)
    _validate_delete_on_verify(spec, request.delete_on_verify)
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
    await _off_loop_bounded(_check_within_volumes, request.file_paths, output_dir)

    # Fast-fail when the queue is already at capacity, before planning any
    # candidate — planning a PS3 folder walks its whole source tree, so a
    # full-queue batch shouldn't pay for that just to be rejected. The
    # projected-depth check below still runs once the surviving candidate count
    # is known (for the "would exceed" case where the queue isn't yet full).
    max_depth = max(0, int(getattr(settings, "max_queue_depth", 0) or 0))
    if 0 < max_depth <= job_manager.get_queue_depth():
        raise HTTPException(
            status_code=429,
            detail=f"Conversion queue full ({max_depth} jobs). Retry later.",
        )

    skipped: dict[str, str] = {}
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
        except SkipFile as skip:
            skipped[file_path] = _SKIP_HTTP.get(skip.reason, (0, skip.reason.value))[1]
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
        loser = existing
        if plan.priority > existing.priority:
            selected[key] = plan
        else:
            loser = plan
        skipped[loser.file_path] = (
            f"Same output as {os.path.basename(selected[key].file_path)}"
        )

    return PreparedBatch(
        request=request,
        compression=compression,
        plans=[selected[key] for key in order],
        skipped=skipped,
    )


def checked_compression(mode: str, compression: str | None) -> str | None:
    """A saved compression for ``mode``, if the mode accepts it.

    A saved value the mode would refuse (left over from an older picker) falls
    back to the tool default rather than failing the whole batch. Used for
    Auto targets and by the CLI (#306).
    """
    if not compression:
        return None
    try:
        value = normalize_compression(compression)
        _validate_request_compression(registry.spec(mode), mode, value)
    except HTTPException:
        logger.warning(
            "Saved compression %r does not fit %s; using the tool default",
            compression, mode,
        )
        return None
    return value


def _checked_auto_plan(file_paths: list[str], **plan_kwargs) -> list:
    """The Volume checks, then ``plan_auto``: one blocking unit, off the loop."""
    _check_within_volumes(file_paths)
    return plan_auto(file_paths, **plan_kwargs)


async def _plan_auto(request: AutoPlanRequest) -> list:
    prefs = await preferences_store.get(CONVERSION_PREFS_KEY) or {}
    output_dir = normalize_output_dir(request.output_dir)
    # The shared, bounded readiness probe (a tool that cannot run here, such as
    # nsz without prod.keys, is skipped in the plan instead of queued to fail).
    ready = await registry.ready_tool_ids()
    # Every filesystem touch (the Volume checks and planning's header reads)
    # runs together off the event loop, bounded: a Volume that stops answering
    # gets a 503, never a frozen API.
    plan = await _off_loop_bounded(
        _checked_auto_plan, list(request.file_paths),
        compression_prefs=prefs, output_dir=output_dir, ready_tool_ids=ready,
    )
    return _with_checked_compression(plan)


async def plan_auto_targets(
    paths: list[str],
    output_dir: str | None,
    prefs: dict,
    *,
    ready_tool_ids: list[str] | None = None,
) -> list:
    """``plan_auto`` with each saved compression checked against its mode.

    The CLI's ``--auto`` (#306); the Auto route plans the same way through
    ``_plan_auto``, which adds the Volume checks and the 30 s bound an HTTP
    request needs. ``prefs`` is the saved ``conversion`` preference and
    ``ready_tool_ids`` the tools that can run here
    (``registry.ready_tool_ids()``). Off the event loop, unbounded: a CLI run
    plans a whole library and has no request to time out.
    """
    plan = await run_in_threadpool(
        plan_auto, paths, compression_prefs=prefs, output_dir=output_dir,
        ready_tool_ids=ready_tool_ids,
    )
    return _with_checked_compression(plan)


def _with_checked_compression(plan: list) -> list:
    return [
        replace(target, compression=checked_compression(target.mode, target.compression))
        if target.mode else target
        for target in plan
    ]


def _plan_item(target) -> AutoPlanItem:
    return AutoPlanItem(
        file_path=target.file_path,
        platform=target.detection.platform,
        how=target.detection.how,
        reason=target.detection.reason,
        mode=target.mode,
        tool_id=target.tool_id,
        compression=target.compression,
        skip_reason=target.skip_reason,
    )


@router.post("/jobs/auto-plan", response_model=list[AutoPlanItem])
async def auto_plan(request: AutoPlanRequest):
    """Each Image's Platform and default target, without queueing anything."""
    return [_plan_item(target) for target in await _plan_auto(request)]


@router.post("/jobs/auto", response_model=AutoBatchResponse)
async def create_auto_jobs(request: AutoJobCreateRequest):
    """Queue the Auto plan the user confirmed.

    ``request.items`` are the confirmed plan's actionable rows, each
    ``{file_path, mode}``; an Image the preview skipped (Conflicted, Unsorted,
    an output another Image writes) is never sent. The paths are planned
    again, and if any item's target differs from the confirmed one (another
    tool, or now skipped) the request is refused with 409 and
    ``detail = {"message", "plan"}``, the new plan, before anything is
    queued: the client shows it and asks again. Queueing what the user never
    saw would also skip its RomM metadata snapshot.

    Images are grouped by target mode and each group goes through the ordinary
    batch planning, so validation, duplicate handling and backpressure are the
    same as a hand-picked batch. An Image preparation skips is reported in the
    returned plan.

    Every group is prepared before any is queued, so a planning error queues
    nothing. Each group is still its own atomic queue call, so if a later group
    is refused at queue time (another job took a destination, a volume stopped
    answering) the jobs this request already queued are withdrawn and the error
    is returned. The rollback waits for running jobs to settle
    (``_cancel_queued``); a job that completed with an output, or is still
    running at the bound, is kept, logged, and returned in the error as
    ``detail = {"message", "jobs"}``, with
    ``jobs`` in the same shape a success returns.
    """
    confirmed: dict[str, str] = {}
    for item in request.items:
        if item.file_path in confirmed:
            raise HTTPException(
                status_code=400, detail=f"Listed twice in the Auto plan: {item.file_path}",
            )
        confirmed[item.file_path] = item.mode
    if not confirmed:
        return AutoBatchResponse(plan=[], jobs=[])
    plan = await _plan_auto(AutoPlanRequest(
        file_paths=list(confirmed), output_dir=request.output_dir,
    ))
    if any(target.mode != confirmed.get(target.file_path) for target in plan) or (
        {target.file_path for target in plan} != set(confirmed)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The plan changed since you confirmed it. Review it and confirm again.",
                "plan": [_plan_item(target).model_dump(mode="json") for target in plan],
            },
        )
    groups: dict[tuple[str, str | None], list[str]] = {}
    for target in plan:
        if target.mode:
            groups.setdefault((target.mode, target.compression), []).append(
                target.file_path,
            )

    prepared = [
        await prepare_batch(BatchJobCreateRequest(
            file_paths=paths,
            mode=ConversionMode(mode),
            output_dir=request.output_dir,
            duplicate_action=request.duplicate_action,
            compression=compression,
        ))
        for (mode, compression), paths in groups.items()
    ]
    _check_queue_room(sum(len(batch.plans) for batch in prepared))
    # An Image preparation skipped (its output appeared after planning, say)
    # is reported in the plan, not shown as headed for a target it never got.
    skipped_now = {
        path: reason for batch in prepared for path, reason in batch.skipped.items()
    }
    plan = [
        replace(t, mode=None, tool_id=None, compression=None, skip_reason=skipped_now[t.file_path])
        if t.mode and t.file_path in skipped_now else t
        for t in plan
    ]

    jobs: list[ConversionJob] = []
    for batch in prepared:
        try:
            jobs += await queue_prepared(batch)
        except HTTPException as exc:
            kept = await _cancel_queued(jobs)
            if kept:
                # The kept jobs go back in the shape a success returns them,
                # so the client can adopt them and keep their re-pin records
                # instead of retiring every snapshot it took for this batch.
                names = ", ".join(job.filename or job.id for job in kept)
                exc.detail = {
                    "message": f"{exc.detail} Kept (finished or still running): {names}.",
                    "jobs": [job.model_dump(mode="json") for job in kept],
                }
            raise
        except Exception as exc:
            # An unexpected failure: withdraw, log the original error with its
            # traceback, and still hand back any job that finished, in the
            # same shape as above, so the client keeps its re-pin records.
            kept = await _cancel_queued(jobs)
            logger.error(
                "Auto batch failed after %d job(s) were queued", len(jobs), exc_info=exc,
            )
            if not kept:
                raise
            names = ", ".join(job.filename or job.id for job in kept)
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Auto batch failed. Kept (finished or still running): {names}.",
                    "jobs": [job.model_dump(mode="json") for job in kept],
                },
            ) from exc
        except BaseException:
            # A client disconnect's CancelledError and the like: still withdraw
            # what this request queued, then let it propagate.
            await _cancel_queued(jobs)
            raise
    return AutoBatchResponse(plan=[_plan_item(t) for t in plan], jobs=jobs)


async def _cancel_queued(jobs: list) -> list:
    """Withdraw jobs an Auto request queued before a later group failed.

    Returns the jobs that were not withdrawn, judged by their real final
    states, never by ``cancel_job()``'s answer: for a processing job that only
    means its cancel event is set, and a worker past its last check (in
    ``post_convert``, say) still completes. So every job is asked to cancel,
    then the rollback waits for them to settle, for at most the job progress
    timeout (``CHD_PROGRESS_TIMEOUT``) or ``ROLLBACK_WAIT_CAP_SECONDS``,
    whichever is shorter. The cap applies even when the timeout is 0 (no job
    bound), because this runs inside the HTTP request. Retained: a job that
    completed with an output, and one still running at the bound (its output
    may yet land). Cancelled and failed jobs are withdrawn. Nothing is deleted.
    """
    for job in jobs:
        try:
            await job_manager.cancel_job(job.id)
        except Exception:  # keep withdrawing the rest; judged by state below
            logger.warning("Could not cancel Auto job %s", job.id, exc_info=True)
    job_bound = max(0, int(getattr(settings, "progress_timeout", 0) or 0))
    bound = min(job_bound, ROLLBACK_WAIT_CAP_SECONDS) if job_bound else ROLLBACK_WAIT_CAP_SECONDS
    await job_manager.wait_until_terminal([job.id for job in jobs], bound)

    kept: list = []
    for job in jobs:
        state = job_manager.get_job(job.id) or job
        status = getattr(state, "status", None)
        name = getattr(state, "filename", None) or job.id
        if status == JobStatus.COMPLETED and getattr(state, "output_path", None):
            logger.warning(
                "Auto job %s (%s) finished before its batch could withdraw it; "
                "its output is kept", job.id, name,
            )
            kept.append(state)
        elif status in (JobStatus.QUEUED, JobStatus.PROCESSING):
            logger.warning(
                "Auto job %s (%s) was still running %ss after its batch failed; "
                "it is kept, and its output may still land", job.id, name, bound,
            )
            kept.append(state)
    return kept


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
        # One-time snapshot of every known job on (re)connection, so the
        # client never has to do a separate /api/jobs round-trip to hydrate.
        # This eliminates the race that existed when a client refreshed the
        # snapshot before subscribing: a job that transitioned to a terminal
        # status (complete / error / cancelled) in the gap would never emit
        # its event over SSE, leaving the client stuck on stale state.
        #
        # Order matters: subscribe to active jobs BEFORE emitting the
        # snapshot. If a queued/processing job reaches a terminal status
        # during snapshot emission, the subscriber queue will hold the
        # terminal event for delivery on the next loop iteration. If we
        # emitted the snapshot first and a job completed in between, the
        # subscription pass would skip it (status no longer matches the
        # QUEUED/PROCESSING filter), and the terminal event would be lost.
        #
        # The snapshot event payload mirrors the live-update shape so the
        # client can apply both through the same handler; legacy clients
        # that don't subscribe to "snapshot" events drop them silently (SSE
        # listener semantics), preserving backwards compatibility.
        snapshot_sent = False
        # Last `history` state pushed to this client. The history cap deletes
        # finished jobs behind the client's back, so a client that only ever
        # sees live jobs has no way to know its Completed/Failed counts have
        # stopped tracking reality. Emitting the evicted totals whenever they
        # change (absolute values, never deltas, so a dropped frame or a
        # reconnect can't skew the count) keeps them true. `last_seq` is the
        # client's cursor into the eviction log: it rides along so each event
        # also names the jobs evicted since the previous one, which the client
        # must drop or it would count them twice (stale row + tally).
        last_history = None
        last_seq = None

        try:
            while True:
                try:
                    # Subscribe to any new jobs FIRST.
                    for job in job_manager.get_all_jobs():
                        if job.id not in queues and job.status in (
                            JobStatus.QUEUED,
                            JobStatus.PROCESSING,
                        ):
                            queues[job.id] = job_manager.subscribe(job.id)

                    # Then emit the one-time snapshot of every known job.
                    if not snapshot_sent:
                        # Cursor captured BEFORE the first snapshot frame goes
                        # out. Emitting the snapshot suspends this generator
                        # between jobs, so a job can be evicted after the
                        # client has already been handed its row; opening the
                        # cursor here means the first `history` frame names it
                        # and the client drops it, instead of counting it as
                        # both a retained row and an evicted job.
                        last_seq = job_manager.history_eviction_seq()
                        for job in job_manager.get_all_jobs():
                            yield {
                                "event": "snapshot",
                                "data": json.dumps(
                                    {
                                        "type": "snapshot",
                                        "job": job.model_dump(mode="json"),
                                    },
                                ),
                            }
                        snapshot_sent = True

                    # Emit the evicted-history totals on connect and on every
                    # subsequent change. Legacy clients that don't listen for
                    # "history" drop it silently, as with "snapshot".
                    history = job_manager.get_history_overflow(
                        since=last_seq, generation=job_manager.history_generation
                    )
                    counts = (history["evicted"], history["seq"])
                    if counts != last_history:
                        last_history = counts
                        last_seq = history["seq"]
                        yield {
                            "event": "history",
                            "data": json.dumps({"type": "history", "history": history}),
                        }

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


@router.get("/jobs/history-overflow")
async def job_history_overflow(since: Optional[int] = None, generation: Optional[str] = None):
    """Counts of finished jobs already evicted by the MAX_JOB_HISTORY cap.

    /api/jobs can only return retained jobs, so a client counting that list
    reports at most ``max_job_history`` no matter how much work finishes.
    Adding these evicted counts to it gives the real total. Also pushed over
    the job event stream as a `history` event, so a connected client stays
    current without polling this.

    Pass ``since`` (a previously returned ``seq``) to also get the ids evicted
    after that point. A poll-only client needs them: it reads /api/jobs and
    this endpoint at two different moments, and a job evicted in between is
    still in the job list *and* in these counts — double-counted — unless it
    can be named and dropped. Read the job list first, then pass the cursor
    here, and the two reads reconcile.

    Pass ``generation`` with it — the generation the cursor came from. A cursor
    minted by an earlier process means nothing to this one, and reading it
    literally would return no ids at all, which is exactly the double-count the
    cursor exists to prevent.
    """
    return job_manager.get_history_overflow(since=since, generation=generation)


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
    confirmation = request.headers.get(ACTION_CONFIRM_HEADER, "")
    if confirmation != CONFIRM_CLEAR_COMPLETED_JOBS:
        raise HTTPException(
            status_code=400,
            detail="Missing confirmation header for clear-completed action",
        )

    deleted_ids = []
    for job in list(job_manager.get_all_jobs()):
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            if await job_manager.delete_job(job.id):
                deleted_ids.append(job.id)
    # Clear wipes history wholesale, so the record of what the cap evicted
    # goes with it: otherwise the tab badges would still count jobs that are
    # gone from every list, on an empty Completed tab. Report those alongside
    # the deleted rows — with history capped at 500, clearing a 1,153-job run
    # deletes 500 rows but clears 1,153 jobs' worth of history, and a
    # "Removed 500" toast under a "Remove 1,153?" prompt reads like a failure.
    forgotten = job_manager.history_overflow_total()
    history_seq = job_manager.reset_history_overflow()
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Clear completed requested from %s; deleted=%d history_forgotten=%d",
        client_host,
        len(deleted_ids),
        forgotten,
    )
    return {
        "deleted": deleted_ids,
        "count": len(deleted_ids),
        "history_forgotten": forgotten,
        "total_cleared": len(deleted_ids) + forgotten,
        # Post-reset cursor, so the caller can discard a history read that was
        # already in flight when this cleared — it would restore the tally.
        # The generation rides along because the sequence is only comparable
        # within one: a backend restart mid-request resets it to a small
        # number that would otherwise read as stale.
        "history_seq": history_seq,
        "history_generation": job_manager.history_generation,
    }


@router.post("/jobs/cancel-all")
async def cancel_all_jobs(request: Request):
    """Cancel all queued and processing jobs."""
    confirmation = request.headers.get(ACTION_CONFIRM_HEADER, "")
    if confirmation != CONFIRM_CANCEL_ALL_JOBS:
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
