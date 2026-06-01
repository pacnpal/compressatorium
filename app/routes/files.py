import logging
import os
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
from utils.path_utils import (
    ensure_path_within_volumes,
    get_volume_name_for_path,
    is_within_configured_volumes,
)

router = APIRouter()
logger = logging.getLogger("chd.files")


def _is_macos_metadata_entry(name: str) -> bool:
    return name == ".DS_Store" or name.startswith("._") or name == "__MACOSX"


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
                if _is_macos_metadata_entry(item):
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
                        is_convertible = "chdman" in convertible_by
                        is_dolphin_convertible = "dolphin" in convertible_by
                        is_z3ds_convertible = "z3ds" in convertible_by
                        is_nsz_convertible = "nsz" in convertible_by
                        chd_status = by_tool.get("chdman")
                        dolphin_status = by_tool.get("dolphin")
                        z3ds_status = by_tool.get("z3ds")
                        nsz_status = by_tool.get("nsz")
                        has_chd = chd_status is not None
                        chd_ready = chd_status.exists if chd_status else False
                        has_rvz = dolphin_status is not None
                        dolphin_ready = dolphin_status.exists if dolphin_status else False
                        dolphin_path = dolphin_status.path if dolphin_status else None
                        has_z3ds = z3ds_status is not None
                        z3ds_ready = z3ds_status.exists if z3ds_status else False
                        z3ds_path = z3ds_status.path if z3ds_status else None
                        has_nsz = nsz_status is not None
                        nsz_ready = nsz_status.exists if nsz_status else False
                        nsz_path = nsz_status.path if nsz_status else None

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
                            has_chd = archive_has_chd > 0

                        entry = FileEntry(
                            name=item,
                            path=item_path,
                            type="archive" if is_archive else "file",
                            size=size,
                            extension=ext,
                            convertible=is_convertible,
                            has_chd=has_chd,
                            has_rvz=has_rvz,
                            dolphin_ready=dolphin_ready,
                            dolphin_path=dolphin_path,
                            chd_ready=chd_ready,
                            dolphin_convertible=is_dolphin_convertible,
                            z3ds_convertible=is_z3ds_convertible,
                            has_z3ds=has_z3ds,
                            z3ds_ready=z3ds_ready,
                            z3ds_path=z3ds_path,
                            nsz_convertible=is_nsz_convertible,
                            has_nsz=has_nsz,
                            nsz_ready=nsz_ready,
                            nsz_path=nsz_path,
                            archive_items=archive_items,
                            archive_has_chd=archive_has_chd,
                            archive_truncated=archive_truncated
                            if is_archive and summarize_archives_flag
                            else None,
                            convertible_by=convertible_by,
                            outputs=outputs,
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
                    if _is_macos_metadata_entry(item):
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
                                is_chd_convertible = "chdman" in convertible_by
                                is_dolphin_convertible = "dolphin" in convertible_by
                                is_z3ds_convertible = "z3ds" in convertible_by
                                is_nsz_convertible = "nsz" in convertible_by
                                chd_status = by_tool.get("chdman")
                                dolphin_status = by_tool.get("dolphin")
                                z3ds_status = by_tool.get("z3ds")
                                nsz_status = by_tool.get("nsz")
                                chd_path = (
                                    str(Path(item_path).with_suffix(".chd"))
                                    if is_chd_convertible
                                    else None
                                )
                                has_chd = chd_status is not None
                                chd_ready = chd_status.exists if chd_status else False
                                has_rvz = dolphin_status is not None
                                dolphin_ready = (
                                    dolphin_status.exists if dolphin_status else False
                                )
                                dolphin_path = (
                                    dolphin_status.path if dolphin_status else None
                                )
                                has_z3ds = z3ds_status is not None
                                z3ds_ready = z3ds_status.exists if z3ds_status else False
                                z3ds_path = z3ds_status.path if z3ds_status else None
                                has_nsz = nsz_status is not None
                                nsz_ready = nsz_status.exists if nsz_status else False
                                nsz_path = nsz_status.path if nsz_status else None
                                files.append(
                                    {
                                        "name": item,
                                        "path": item_path,
                                        "size": os.path.getsize(item_path),
                                        "extension": ext,
                                        "chd_path": chd_path,
                                        "has_chd": has_chd,
                                        "has_rvz": has_rvz,
                                        "dolphin_ready": dolphin_ready,
                                        "dolphin_path": dolphin_path,
                                        "chd_ready": chd_ready,
                                        "convertible": is_chd_convertible,
                                        "dolphin_convertible": is_dolphin_convertible,
                                        "z3ds_convertible": is_z3ds_convertible,
                                        "has_z3ds": has_z3ds,
                                        "z3ds_ready": z3ds_ready,
                                        "z3ds_path": z3ds_path,
                                        "nsz_convertible": is_nsz_convertible,
                                        "has_nsz": has_nsz,
                                        "nsz_ready": nsz_ready,
                                        "nsz_path": nsz_path,
                                        "in_archive": False,
                                        "convertible_by": convertible_by,
                                        "outputs": outputs,
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
                                    archives.append(
                                        {
                                            "name": entry["name"],
                                            "path": f"{item_path}::{entry['internal_path']}",
                                            "archive_path": item_path,
                                            "internal_path": entry["internal_path"],
                                            "size": entry["size"],
                                            "extension": entry["extension"],
                                            "output_stem": output_stem,
                                            "chd_path": chd_path,
                                            "has_chd": file_exists or is_converting,
                                            "has_rvz": False,
                                            "dolphin_ready": False,
                                            "dolphin_path": None,
                                            "chd_ready": file_exists,
                                            "convertible": (
                                                entry.get("extension")
                                                in registry.get("chdman").input_extensions
                                            ),
                                            "dolphin_convertible": False,
                                            "z3ds_ready": False,
                                            "in_archive": True,
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

    # Check for existing CHD files in the archive's directory
    archive_dir = os.path.dirname(path)
    for file_entry in contents:
        # Get the base name without extension and add .chd
        base_name = file_entry.get("output_stem") or Path(file_entry["name"]).stem
        chd_path = os.path.join(archive_dir, f"{base_name}.chd")
        # Use atomic check to get both file existence and lock status
        file_exists, is_converting = lock_manager.check_file_status(chd_path)
        file_entry["has_chd"] = file_exists or is_converting
        file_entry["chd_ready"] = file_exists
        file_entry["chd_path"] = chd_path

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
) -> dict:
    """Delete a file or empty directory."""
    if not await run_in_threadpool(is_within_configured_volumes, path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes",
        )

    if not await run_in_threadpool(os.path.exists, path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    is_dir = await run_in_threadpool(os.path.isdir, path)
    await _assert_path_not_in_use(path, is_dir=is_dir)

    try:
        if is_dir:
            # Only delete empty directories for safety
            contents = await run_in_threadpool(os.listdir, path)
            if contents:
                raise HTTPException(
                    status_code=400, detail="Cannot delete non-empty directory",
                )
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
