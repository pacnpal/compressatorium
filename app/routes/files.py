import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from config import settings
from models import FileEntry, DirectoryListing, Volume, BulkDeleteRequest
from services.chdman import CONVERTIBLE_EXTENSIONS
from services.archive import archive_service, ARCHIVE_EXTENSIONS
from services.lock_manager import lock_manager
from services.verification_store import verification_store
from utils.path_utils import is_within_configured_volumes, get_volume_name_for_path

router = APIRouter()


@router.get("/volumes", response_model=List[Volume])
async def list_volumes():
    """List all configured volume mount points."""
    volumes = []
    for vol_path in settings.volumes:
        if os.path.isdir(vol_path):
            volumes.append(
                Volume(name=settings.get_volume_name(vol_path), path=vol_path)
            )
    return volumes


@router.get("/files", response_model=DirectoryListing)
async def list_files(
    path: str = Query(..., description="Directory path to list"),
    show_archives: bool = Query(True, description="Show archive contents"),
    summarize_archives: bool = Query(True, description="Include archive summaries"),
):
    """List files in a directory."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Directory not found")

    # Determine which volume this path belongs to
    volume_name = get_volume_name_for_path(path) or ""

    def scan_directory(
        target_path: str, show_archives_flag: bool, summarize_archives_flag: bool
    ):
        entries = []
        try:
            for item in sorted(os.listdir(target_path)):
                item_path = os.path.join(target_path, item)
                ext = Path(item).suffix.lower()

                if os.path.isdir(item_path):
                    entries.append(
                        FileEntry(name=item, path=item_path, type="directory")
                    )
                elif os.path.isfile(item_path):
                    size = os.path.getsize(item_path)
                    is_convertible = ext in CONVERTIBLE_EXTENSIONS
                    is_archive = ext in ARCHIVE_EXTENSIONS
                    if is_archive and not show_archives_flag:
                        continue

                    # Check if CHD already exists or is being converted (atomic check)
                    has_chd = False
                    if is_convertible:
                        chd_path = str(Path(item_path).with_suffix(".chd"))
                        file_exists, is_converting = lock_manager.check_file_status(
                            chd_path
                        )
                        has_chd = file_exists or is_converting

                    archive_items = None
                    archive_has_chd = None
                    if is_archive and summarize_archives_flag:
                        contents = archive_service.list_archive_contents(item_path)
                        archive_items = len(contents)
                        archive_has_chd = 0
                        archive_dir = os.path.dirname(item_path)
                        for entry in contents:
                            output_stem = (
                                entry.get("output_stem")
                                or Path(entry["internal_path"]).stem
                            )
                            chd_path = os.path.join(archive_dir, f"{output_stem}.chd")
                            file_exists, is_converting = lock_manager.check_file_status(
                                chd_path
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
                        archive_items=archive_items,
                        archive_has_chd=archive_has_chd,
                    )
                    entries.append(entry)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        return entries

    entries = await run_in_threadpool(
        scan_directory, path, show_archives, summarize_archives
    )

    return DirectoryListing(volume=volume_name, path=path, entries=entries)


@router.get("/files/search")
async def search_files(
    path: str = Query(..., description="Root path to search"),
    recursive: bool = Query(True, description="Search subdirectories"),
    include_archives: bool = Query(True, description="Search inside archives"),
) -> dict:
    """Search for convertible files in a directory tree."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Directory not found")

    def scan_directory(
        root_path: str, recursive_scan: bool, include_archive_scan: bool
    ):
        files = []
        archives = []
        visited_dirs = set()

        def _scan(dir_path: str):
            real_dir = os.path.realpath(dir_path)
            if real_dir in visited_dirs:
                return
            visited_dirs.add(real_dir)
            try:
                for item in os.listdir(dir_path):
                    item_path = os.path.join(dir_path, item)
                    ext = Path(item).suffix.lower()

                    if os.path.isdir(item_path):
                        if recursive_scan and not os.path.islink(item_path):
                            _scan(item_path)
                    elif os.path.isfile(item_path):
                        if ext in CONVERTIBLE_EXTENSIONS:
                            chd_path = str(Path(item_path).with_suffix(".chd"))
                            # Use atomic check to get both file existence and lock status
                            file_exists, is_converting = lock_manager.check_file_status(
                                chd_path
                            )
                            files.append(
                                {
                                    "name": item,
                                    "path": item_path,
                                    "size": os.path.getsize(item_path),
                                    "extension": ext,
                                    "has_chd": file_exists or is_converting,
                                    "in_archive": False,
                                }
                            )
                        elif include_archive_scan and ext in ARCHIVE_EXTENSIONS:
                            # List archive contents
                            archive_contents = archive_service.list_archive_contents(
                                item_path
                            )
                            archive_dir = os.path.dirname(item_path)
                            for entry in archive_contents:
                                output_stem = (
                                    entry.get("output_stem")
                                    or Path(entry["internal_path"]).stem
                                )
                                chd_path = os.path.join(
                                    archive_dir, f"{output_stem}.chd"
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
                                        "in_archive": True,
                                    }
                                )
            except PermissionError:
                pass

        _scan(root_path)
        return files, archives

    files, archives = await run_in_threadpool(
        scan_directory, path, recursive, include_archives
    )

    return {
        "root": path,
        "files": files,
        "archives": archives,
        "total_files": len(files),
        "total_in_archives": len(archives),
    }


@router.get("/files/archive")
async def list_archive(
    path: str = Query(..., description="Path to archive file"),
) -> dict:
    """List convertible files inside an archive."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Archive not found")

    if not archive_service.is_archive(path):
        raise HTTPException(status_code=400, detail="Not a supported archive format")

    contents = await run_in_threadpool(archive_service.list_archive_contents, path)

    # Check for existing CHD files in the archive's directory
    archive_dir = os.path.dirname(path)
    for file_entry in contents:
        # Get the base name without extension and add .chd
        base_name = file_entry.get("output_stem") or Path(file_entry["name"]).stem
        chd_path = os.path.join(archive_dir, f"{base_name}.chd")
        # Use atomic check to get both file existence and lock status
        file_exists, is_converting = lock_manager.check_file_status(chd_path)
        file_entry["has_chd"] = file_exists or is_converting
        file_entry["chd_path"] = chd_path

    return {"archive": path, "files": contents, "total": len(contents)}


@router.post("/files/rename")
async def rename_file(
    path: str = Query(..., description="Path to file or directory to rename"),
    new_name: str = Query(..., description="New name for the file or directory"),
) -> dict:
    """Rename a file or directory."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    # Validate new name (no path separators, no empty, no special chars that could be problematic)
    if not new_name or "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid new name")

    parent_dir = os.path.dirname(path)
    new_path = os.path.join(parent_dir, new_name)

    # Check if new path is also within allowed volumes
    if not is_within_configured_volumes(new_path, treat_archives=False):
        raise HTTPException(
            status_code=403,
            detail="Access denied: target path outside configured volumes",
        )

    # Check if target already exists
    if os.path.exists(new_path):
        raise HTTPException(
            status_code=409, detail="A file or directory with that name already exists"
        )

    try:
        os.rename(path, new_path)
        old_is_chd = path.lower().endswith(".chd")
        new_is_chd = new_path.lower().endswith(".chd")
        if old_is_chd and new_is_chd:
            verification_store.move(path, new_path)
        elif old_is_chd and not new_is_chd:
            verification_store.clear(path)
        return {
            "success": True,
            "old_path": path,
            "new_path": new_path,
            "message": f"Successfully renamed to {new_name}",
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename: {str(e)}")


@router.delete("/files/delete")
async def delete_file(
    path: str = Query(..., description="Path to file or directory to delete"),
) -> dict:
    """Delete a file or empty directory."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(
            status_code=403, detail="Access denied: path outside configured volumes"
        )

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File or directory not found")

    try:
        if os.path.isdir(path):
            # Only delete empty directories for safety
            if os.listdir(path):
                raise HTTPException(
                    status_code=400, detail="Cannot delete non-empty directory"
                )
            os.rmdir(path)
        else:
            os.remove(path)
            if path.lower().endswith(".chd"):
                verification_store.clear(path)

        return {"success": True, "path": path, "message": "Successfully deleted"}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")


@router.post("/files/delete-batch")
async def delete_files_batch(request: BulkDeleteRequest) -> dict:
    """Delete multiple files at once."""
    if not request.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    results = []
    success_count = 0
    failed_count = 0

    def delete_single_file(path: str) -> dict:
        """Delete a single file and return result."""
        # Validate path is within configured volumes
        if not is_within_configured_volumes(path, treat_archives=False):
            return {
                "path": path,
                "success": False,
                "error": "Access denied: path outside configured volumes",
            }

        if not os.path.exists(path):
            return {
                "path": path,
                "success": False,
                "error": "File or directory not found",
            }

        try:
            if os.path.isdir(path):
                # Only delete empty directories for safety
                if os.listdir(path):
                    return {
                        "path": path,
                        "success": False,
                        "error": "Cannot delete non-empty directory",
                    }
                os.rmdir(path)
            else:
                os.remove(path)
                if path.lower().endswith(".chd"):
                    verification_store.clear(path)

            return {"path": path, "success": True}
        except OSError as e:
            return {"path": path, "success": False, "error": str(e)}

    # Process all files
    for path in request.paths:
        result = await run_in_threadpool(delete_single_file, path)
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
