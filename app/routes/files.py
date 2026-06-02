from logging_setup import get_logger
import os
import shutil
from pathlib import Path

from config import settings
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from models import BulkDeleteRequest, DirectoryListing, FileEntry, OutputStatus, Volume
from services.archive import ARCHIVE_EXTENSIONS, archive_service
from services.chd_metadata_store import chd_metadata_store
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.tools import registry
from services.verification_store import verification_store
from utils.junk import is_junk_entry
from utils.path_utils import (
    ensure_path_within_volumes,
    get_volume_name_for_path,
    is_within_configured_volumes,
)

router = APIRouter()
logger = get_logger("files")


def _detect_file_outputs(
    item_path: str, ext: str,
) -> tuple[list[str], list[OutputStatus], dict[str, OutputStatus]]:
    """Drive convertibility + output detection off the tool registry.

    Returns the tool ids that accept this input, the detected sibling
    outputs, and a ``{tool_id: OutputStatus}`` index used to derive the
    legacy per-tool booleans from a single source of truth.
    """
    convertible_by: list[str] = []
    outputs: list[OutputStatus] = []
    by_tool: dict[str, OutputStatus] = {}
    for tool in registry.all():
        if ext in tool.input_extensions:
            convertible_by.append(tool.id)
        status = tool.detect_output(item_path)
        if status is not None:
            outputs.append(status)
            by_tool[tool.id] = status
    return convertible_by, outputs, by_tool


def _detect_archive_member_outputs(
    entry: dict, archive_dir: str,
) -> tuple[str, list[str], list[OutputStatus], dict[str, OutputStatus]]:
    """Registry-driven convertibility + output detection for an archive member.

    The member doesn't exist on disk yet, but each tool's ``detect_output``
    resolves its sibling output by swapping the suffix on the input path. So we
    synthesise the path the extracted member *would* occupy next to its archive
    (``<archive_dir>/<output_stem><ext>``) and reuse the same on-disk detection
    the regular file scan uses. This keeps the listing in lock-step with the
    job pipeline — which extracts the member into the archive's directory before
    converting — and means any newly registered archive-aware tool surfaces in
    the browse/search views without another CHD-style special case.
    """
    output_stem = (
        entry.get("output_stem") or Path(entry["internal_path"]).stem
    )
    member_ext = (entry.get("extension") or Path(entry["name"]).suffix).lower()
    synthetic_input = os.path.join(archive_dir, f"{output_stem}{member_ext}")
    convertible_by, outputs, by_tool = _detect_file_outputs(
        synthetic_input, member_ext,
    )
    return output_stem, convertible_by, outputs, by_tool


def _legacy_output_fields(
    convertible_by: list[str], by_tool: dict[str, OutputStatus],
) -> dict:
    """Derive the legacy per-tool booleans/paths from registry detection.

    Single source of truth shared by the on-disk and archive-member listing
    paths so the flat ``has_*`` / ``*_ready`` / ``*_convertible`` flags the
    frontend still reads can't drift from ``convertible_by`` / ``outputs``.
    """
    fields: dict = {}
    for tool_id, has_key, ready_key, path_key in (
        ("chdman", "has_chd", "chd_ready", None),
        ("dolphin", "has_rvz", "dolphin_ready", "dolphin_path"),
        ("z3ds", "has_z3ds", "z3ds_ready", "z3ds_path"),
        ("nsz", "has_nsz", "nsz_ready", "nsz_path"),
    ):
        status = by_tool.get(tool_id)
        fields[has_key] = status is not None
        fields[ready_key] = status.exists if status else False
        if path_key is not None:
            fields[path_key] = status.path if status else None
    fields["convertible"] = "chdman" in convertible_by
    fields["dolphin_convertible"] = "dolphin" in convertible_by
    fields["z3ds_convertible"] = "z3ds" in convertible_by
    fields["nsz_convertible"] = "nsz" in convertible_by
    return fields


async def _assert_path_not_in_use(path: str, *, is_dir: bool = False) -> None:
    _, is_locked = await run_in_threadpool(lock_manager.check_file_status, path)
    if is_locked:
        raise HTTPException(
            status_code=409, detail="Path is locked by an active conversion",
        )

    candidates = job_manager.get_active_job_candidates()

    def _match_job_id() -> str | None:
        try:
            target = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

        for job_id, paths in candidates:
            for candidate in paths:
                try:
                    cand_path = Path(candidate).expanduser().resolve(strict=False)
                except (OSError, RuntimeError):
                    continue
                if cand_path == target:
                    return job_id
                if is_dir:
                    try:
                        cand_path.relative_to(target)
                        return job_id
                    except ValueError:
                        continue
        return None

    job_id = await run_in_threadpool(_match_job_id)
    if job_id:
        raise HTTPException(
            status_code=409, detail=f"Path is in use by active job {job_id}",
        )


@router.get("/volumes", response_model=list[Volume])
async def list_volumes():
    """List all configured volume mount points."""
    volumes = []
    for vol_path in settings.volumes:
        if os.path.isdir(vol_path):
            volumes.append(
                Volume(name=settings.get_volume_name(vol_path), path=vol_path),
            )
    return volumes


@router.get("/files", response_model=DirectoryListing)
async def list_files(
    path: str = Query(..., description="Directory path to list"),
    show_archives: bool = Query(True, description="Show archive contents"),
    summarize_archives: bool = Query(True, description="Include archive summaries"),
):
    """List files in a directory."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isdir(resolved_str):
        raise HTTPException(status_code=404, detail="Directory not found")

    # Determine which volume this path belongs to
    volume_name = get_volume_name_for_path(path) or ""

    def scan_directory(
        target_path: str, show_archives_flag: bool, summarize_archives_flag: bool,
    ):
        entries = []
        try:
            # This outer try-except catches errors from os.listdir() itself
            for item in sorted(os.listdir(target_path)):
                if is_junk_entry(item):
                    continue
                item_path = os.path.join(target_path, item)
                ext = Path(item).suffix.lower()

                try:
                    if os.path.isdir(item_path):
                        entries.append(
                            FileEntry(name=item, path=item_path, type="directory"),
                        )
                    elif os.path.isfile(item_path):
                        size = os.path.getsize(item_path)
                        is_archive = ext in ARCHIVE_EXTENSIONS
                        archive_truncated = None
                        if is_archive and not show_archives_flag:
                            continue

                        # Registry-driven convertibility + sibling-output
                        # detection; the legacy booleans below are derived
                        # from these so they can't drift. Archives are never
                        # directly convertible (only their members are), so
                        # skip detection for them.
                        if is_archive:
                            convertible_by, outputs, by_tool = [], [], {}
                        else:
                            convertible_by, outputs, by_tool = _detect_file_outputs(
                                item_path, ext,
                            )
                        tool_fields = _legacy_output_fields(convertible_by, by_tool)

                        archive_items = None
                        archive_has_chd = None
                        if is_archive and summarize_archives_flag:
                            archive_result = archive_service.list_archive_contents(
                                item_path, include_meta=True,
                            )
                            contents = archive_result["entries"]
                            archive_items = len(contents)
                            archive_truncated = bool(archive_result["truncated"])
                            archive_has_chd = 0
                            archive_dir = os.path.dirname(item_path)
                            for entry in contents:
                                output_stem = (
                                    entry.get("output_stem")
                                    or Path(entry["internal_path"]).stem
                                )
                                chd_path = os.path.join(
                                    archive_dir, f"{output_stem}.chd",
                                )
                                file_exists, is_converting = (
                                    lock_manager.check_file_status(chd_path)
                                )
                                if file_exists or is_converting:
                                    archive_has_chd += 1
                            tool_fields["has_chd"] = archive_has_chd > 0

                        entry = FileEntry(
                            name=item,
                            path=item_path,
                            type="archive" if is_archive else "file",
                            size=size,
                            extension=ext,
                            archive_items=archive_items,
                            archive_has_chd=archive_has_chd,
                            archive_truncated=archive_truncated
                            if is_archive and summarize_archives_flag
                            else None,
                            convertible_by=convertible_by,
                            outputs=outputs,
                            **tool_fields,
                        )
                        entries.append(entry)
                except OSError:
                    # Skip items that cannot be accessed (permissions, missing, etc.)
                    continue
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Directory not found") from None
        return entries

    entries = await run_in_threadpool(
        scan_directory, path, show_archives, summarize_archives,
    )

    return DirectoryListing(volume=volume_name, path=path, entries=entries)


@router.get("/files/search")
async def search_files(
    path: str = Query(..., description="Root path to search"),
    recursive: bool = Query(True, description="Search subdirectories"),
    include_archives: bool = Query(True, description="Search inside archives"),
) -> dict:
    """Search for convertible files in a directory tree."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isdir(resolved_str):
        raise HTTPException(status_code=404, detail="Directory not found")

    def scan_directory(
        root_path: str, recursive_scan: bool, include_archive_scan: bool,
    ):
        files = []
        archives = []
        visited_dirs = set()
        archives_truncated = set()

        def _scan(dir_path: str):
            real_dir = os.path.realpath(dir_path)
            if real_dir in visited_dirs:
                return
            visited_dirs.add(real_dir)
            try:
                for item in os.listdir(dir_path):
                    if is_junk_entry(item):
                        continue
                    item_path = os.path.join(dir_path, item)
                    ext = Path(item).suffix.lower()

                    try:
                        if os.path.isdir(item_path):
                            if recursive_scan and not os.path.islink(item_path):
                                _scan(item_path)
                        elif os.path.isfile(item_path):
                            is_archive = ext in ARCHIVE_EXTENSIONS
                            if is_archive:
                                convertible_by, outputs, by_tool = [], [], {}
                            else:
                                convertible_by, outputs, by_tool = _detect_file_outputs(
                                    item_path, ext,
                                )
                            if convertible_by:
                                tool_fields = _legacy_output_fields(
                                    convertible_by, by_tool,
                                )
                                chd_path = (
                                    str(Path(item_path).with_suffix(".chd"))
                                    if tool_fields["convertible"]
                                    else None
                                )
                                files.append(
                                    {
                                        "name": item,
                                        "path": item_path,
                                        "size": os.path.getsize(item_path),
                                        "extension": ext,
                                        "chd_path": chd_path,
                                        "in_archive": False,
                                        "convertible_by": convertible_by,
                                        "outputs": outputs,
                                        **tool_fields,
                                    },
                                )
                            elif include_archive_scan and is_archive:
                                # List archive contents
                                archive_result = archive_service.list_archive_contents(
                                    item_path, include_meta=True,
                                )
                                archive_contents = archive_result["entries"]
                                if archive_result["truncated"]:
                                    archives_truncated.add(item_path)
                                archive_dir = os.path.dirname(item_path)
                                for entry in archive_contents:
                                    (
                                        output_stem,
                                        member_convertible_by,
                                        member_outputs,
                                        member_by_tool,
                                    ) = _detect_archive_member_outputs(
                                        entry, archive_dir,
                                    )
                                    tool_fields = _legacy_output_fields(
                                        member_convertible_by, member_by_tool,
                                    )
                                    archives.append(
                                        {
                                            "name": entry["name"],
                                            "path": f"{item_path}::{entry['internal_path']}",
                                            "archive_path": item_path,
                                            "internal_path": entry["internal_path"],
                                            "size": entry["size"],
                                            "extension": entry["extension"],
                                            "output_stem": output_stem,
                                            "chd_path": os.path.join(
                                                archive_dir, f"{output_stem}.chd",
                                            ),
                                            "in_archive": True,
                                            "convertible_by": member_convertible_by,
                                            "outputs": member_outputs,
                                            **tool_fields,
                                        },
                                    )
                    except OSError:
                        # Skip archive entries that cannot be accessed
                        continue
            except (PermissionError, FileNotFoundError, OSError):
                # Directory is not accessible or disappeared during scanning; skip it.
                pass

        _scan(root_path)
        return files, archives, sorted(archives_truncated)

    files, archives, archives_truncated = await run_in_threadpool(
        scan_directory, path, recursive, include_archives,
    )

    return {
        "root": path,
        "files": files,
        "archives": archives,
        "total_files": len(files),
        "total_in_archives": len(archives),
        "archives_truncated": archives_truncated,
    }


@router.get("/files/archive")
async def list_archive(
    path: str = Query(..., description="Path to archive file"),
) -> dict:
    """List convertible files inside an archive."""
    try:
        resolved = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        ) from None

    resolved_str = str(resolved)
    if not os.path.isfile(resolved_str):
        raise HTTPException(status_code=404, detail="Archive not found")

    if not archive_service.is_archive(resolved_str):
        raise HTTPException(status_code=400, detail="Not a supported archive format")

    contents_result = await run_in_threadpool(
        archive_service.list_archive_contents, path, include_meta=True,
    )
    contents = contents_result["entries"]
    truncated = bool(contents_result["truncated"])

    # Registry-driven convertibility + sibling-output detection for each
    # member, mirroring the on-disk file scan so every archive-aware tool
    # (chdman, dolphin, z3ds, nsz, …) gets badged — not just CHDMAN.
    archive_dir = os.path.dirname(path)
    for file_entry in contents:
        output_stem, convertible_by, outputs, by_tool = (
            _detect_archive_member_outputs(file_entry, archive_dir)
        )
        file_entry.update(_legacy_output_fields(convertible_by, by_tool))
        file_entry["output_stem"] = output_stem
        file_entry["chd_path"] = os.path.join(archive_dir, f"{output_stem}.chd")
        file_entry["convertible_by"] = convertible_by
        file_entry["outputs"] = outputs

    return {
        "archive": path,
        "files": contents,
        "total": len(contents),
        "truncated": truncated,
    }


@router.post("/files/rename")
async def rename_file(
    path: str = Query(..., description="Path to file or directory to rename"),
    new_name: str = Query(..., description="New name for the file or directory"),
) -> dict:
    """Rename a file or directory."""
    # is_within_configured_volumes uses os.path.realpath which hits disk
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.exists, path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    is_dir = await run_in_threadpool(os.path.isdir, path)
    await _assert_path_not_in_use(path, is_dir=is_dir)

    # Validate new name (no path separators, no empty, no special chars that could be problematic)
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid new name")

    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_name)

    # Check if new path is also within allowed volumes
    if not await run_in_threadpool(is_within_configured_volumes, new_path, treat_archives=False):
        raise HTTPException(
            status_code=403,
            detail="Access denied: target path outside configured volumes",
        )

    # Check if target already exists
    if await run_in_threadpool(os.path.exists, new_path):
        raise HTTPException(
            status_code=409, detail="A file or directory with that name already exists",
        )
    await _assert_path_not_in_use(new_path, is_dir=False)

    try:
        await run_in_threadpool(os.rename, path, new_path)
        # Path-bookkeeping that mirrors the rename. verification_store
        # records carry verify-class outputs from any registered tool
        # (.chd, .rvz, .wia, .z3ds, .zcci, .zcia, …) so we have to do
        # this for the union, not just CHD. chd_metadata_store is
        # CHD-specific by design.
        #
        # Only carry the verification record across renames within the
        # SAME extension. Cross-format renames (.chd → .rvz, .rvz →
        # .wia) don't actually convert the file content, so the new
        # path was never verified under its new tool/format; carrying
        # the record would teach /api/verified to lie. Clear the old
        # record in that case and require re-verification.
        verify_exts = registry.verify_extensions()
        old_ext = os.path.splitext(path)[1].lower()
        new_ext = os.path.splitext(new_path)[1].lower()
        old_is_verify = old_ext in verify_exts
        new_is_verify = new_ext in verify_exts
        old_is_chd = old_ext == ".chd"
        new_is_chd = new_ext == ".chd"
        if old_is_verify and new_is_verify and old_ext == new_ext:
            await verification_store.move(path, new_path)
        elif old_is_verify:
            # Cross-format rename, or new ext is not a verify class.
            await verification_store.clear(path)
        if old_is_chd and new_is_chd:
            await chd_metadata_store.move(path, new_path)
        elif old_is_chd and not new_is_chd:
            await chd_metadata_store.clear(path)
        return {
            "success": True,
            "old_path": path,
            "new_path": new_path,
            "message": f"Successfully renamed to {new_name}",
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {e!s}") from None


@router.delete("/files/delete")
async def delete_file(
    path: str = Query(..., description="Path to file or directory to delete"),
    recursive: bool = Query(
        False,
        description=(
            "Allow deleting a non-empty directory and everything inside it. "
            "The Web UI only sets this after a double confirmation."
        ),
    ),
) -> dict:
    """Delete a file, an empty directory, or (with ``recursive``) a non-empty one."""
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.exists, path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    is_dir = await run_in_threadpool(os.path.isdir, path)
    # For a recursive directory delete this also rejects the request if any
    # active job's path lives under the directory (see _assert_path_not_in_use).
    await _assert_path_not_in_use(path, is_dir=is_dir)

    try:
        if is_dir:
            contents = await run_in_threadpool(os.listdir, path)
            if contents and not recursive:
                # Non-empty without explicit opt-in: the UI re-requests with
                # recursive=true after a second confirmation.
                raise HTTPException(
                    status_code=409,
                    detail="Directory is not empty; confirm recursive delete to remove it",
                )
            if contents:
                await run_in_threadpool(shutil.rmtree, path)
            else:
                await run_in_threadpool(os.rmdir, path)
        else:
            await run_in_threadpool(os.remove, path)
            ext = os.path.splitext(path)[1].lower()
            if ext in registry.verify_extensions():
                await verification_store.clear(path)
            if ext == ".chd":
                await chd_metadata_store.clear(path)

        return {"success": True, "path": path, "message": "Successfully deleted"}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {e!s}") from None


@router.post("/files/delete-batch")
async def delete_files_batch(request: BulkDeleteRequest) -> dict:
    """Delete multiple files at once."""
    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    results = []
    success_count = 0
    failed_count = 0

    async def delete_single_file(path: str) -> dict:
        """Delete a single file and return result."""
        # Validate path is within configured volumes
        if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
            return {
                "path": path,
                "success": False,
                "error": "Access denied: path outside configured volumes",
            }

        if not await run_in_threadpool(os.path.exists, path):
            return {
                "path": path,
                "success": False,
                "error": "File or directory not found",
            }

        is_dir = await run_in_threadpool(os.path.isdir, path)
        try:
            await _assert_path_not_in_use(path, is_dir=is_dir)
        except HTTPException as exc:
            return {"path": path, "success": False, "error": exc.detail}

        try:
            if is_dir:
                # Only delete empty directories for safety
                # os.listdir can be blocking
                contents = await run_in_threadpool(os.listdir, path)
                if contents:
                    return {
                        "path": path,
                        "success": False,
                        "error": "Cannot delete non-empty directory",
                    }
                await run_in_threadpool(os.rmdir, path)
            else:
                await run_in_threadpool(os.remove, path)
                ext = os.path.splitext(path)[1].lower()
                if ext in registry.verify_extensions():
                    await verification_store.clear(path)
                if ext == ".chd":
                    await chd_metadata_store.clear(path)

            return {"path": path, "success": True}
        except OSError as e:
            logger.warning("Delete failed for %s: %s", path, e)
            return {"path": path, "success": False, "error": "File operation failed"}

    # Process all files
    for path in request.paths:
        result = await delete_single_file(path)
        results.append(result)
        if result["success"]:
            success_count += 1
        else:
            failed_count += 1

    return {
        "total": len(request.paths),
        "success": success_count,
        "failed": failed_count,
        "results": results,
    }
