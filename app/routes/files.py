import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.models import FileEntry, DirectoryListing, Volume
from app.services.chdman import chdman_service, CONVERTIBLE_EXTENSIONS
from app.services.archive import archive_service, ARCHIVE_EXTENSIONS
from app.services.lock_manager import lock_manager

router = APIRouter()


def validate_path(path: str) -> bool:
    """Validate that a path is within configured volumes (prevent traversal)."""
    try:
        # Resolve the user-supplied path (do not require it to exist).
        real_path = Path(path).resolve(strict=False)
    except Exception:
        # If the path cannot be resolved safely, treat it as invalid.
        return False

    for volume in settings.volumes:
        try:
            # Resolve the configured volume path; it should exist.
            real_volume = Path(volume).resolve(strict=True)
        except Exception:
            # Skip misconfigured or non-existent volumes.
            continue

        # Prefer pathlib.Path.is_relative_to when available (Python 3.9+).
        is_inside = False
        try:
            # is_relative_to returns True if real_path is within real_volume.
            is_inside = real_path.is_relative_to(real_volume)
        except AttributeError:
            # Fallback for older Python versions: use os.path.commonpath.
            real_path_str = str(real_path)
            real_volume_str = str(real_volume)
            try:
                common = os.path.commonpath([real_path_str, real_volume_str])
            except ValueError:
                # Different drive letters on Windows, etc.
                common = ""
            is_inside = common == real_volume_str

        if is_inside:
            return True
    return False


@router.get("/volumes", response_model=List[Volume])
async def list_volumes():
    """List all configured volume mount points."""
    volumes = []
    for vol_path in settings.volumes:
        if os.path.isdir(vol_path):
            volumes.append(Volume(
                name=settings.get_volume_name(vol_path),
                path=vol_path
            ))
    return volumes


@router.get("/files", response_model=DirectoryListing)
async def list_files(
    path: str = Query(..., description="Directory path to list"),
    show_archives: bool = Query(True, description="Show archive contents")
):
    """List files in a directory."""
    if not validate_path(path):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Directory not found")

    # Determine which volume this path belongs to
    volume_name = ""
    for vol_path in settings.volumes:
        real_vol = os.path.realpath(vol_path)
        real_path = os.path.realpath(path)
        if real_path.startswith(real_vol):
            volume_name = settings.get_volume_name(vol_path)
            break

    entries = []

    try:
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            ext = Path(item).suffix.lower()

            if os.path.isdir(item_path):
                entries.append(FileEntry(
                    name=item,
                    path=item_path,
                    type="directory"
                ))
            elif os.path.isfile(item_path):
                size = os.path.getsize(item_path)
                is_convertible = ext in CONVERTIBLE_EXTENSIONS
                is_archive = ext in ARCHIVE_EXTENSIONS

                # Check if CHD already exists or is being converted (atomic check)
                has_chd = False
                if is_convertible:
                    chd_path = str(Path(item_path).with_suffix(".chd"))
                    # Use atomic check to get both file existence and lock status
                    file_exists, is_converting = lock_manager.check_file_status(chd_path)
                    has_chd = file_exists or is_converting

                entry = FileEntry(
                    name=item,
                    path=item_path,
                    type="archive" if is_archive else "file",
                    size=size,
                    extension=ext,
                    convertible=is_convertible,
                    has_chd=has_chd
                )
                entries.append(entry)

    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return DirectoryListing(
        volume=volume_name,
        path=path,
        entries=entries
    )


@router.get("/files/search")
async def search_files(
    path: str = Query(..., description="Root path to search"),
    recursive: bool = Query(True, description="Search subdirectories"),
    include_archives: bool = Query(True, description="Search inside archives")
) -> dict:
    """Search for convertible files in a directory tree."""
    if not validate_path(path):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Directory not found")

    files = []
    archives = []

    def scan_directory(dir_path: str):
        try:
            for item in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item)
                ext = Path(item).suffix.lower()

                if os.path.isdir(item_path):
                    if recursive:
                        scan_directory(item_path)
                elif os.path.isfile(item_path):
                    if ext in CONVERTIBLE_EXTENSIONS:
                        chd_path = str(Path(item_path).with_suffix(".chd"))
                        # Use atomic check to get both file existence and lock status
                        file_exists, is_converting = lock_manager.check_file_status(chd_path)
                        files.append({
                            "name": item,
                            "path": item_path,
                            "size": os.path.getsize(item_path),
                            "extension": ext,
                            "has_chd": file_exists or is_converting,
                            "in_archive": False
                        })
                    elif include_archives and ext in ARCHIVE_EXTENSIONS:
                        # List archive contents
                        archive_contents = archive_service.list_archive_contents(item_path)
                        for entry in archive_contents:
                            archives.append({
                                "name": entry["name"],
                                "path": f"{item_path}::{entry['internal_path']}",
                                "archive_path": item_path,
                                "internal_path": entry["internal_path"],
                                "size": entry["size"],
                                "extension": entry["extension"],
                                "has_chd": False,
                                "in_archive": True
                            })
        except PermissionError:
            pass

    scan_directory(path)

    return {
        "root": path,
        "files": files,
        "archives": archives,
        "total_files": len(files),
        "total_in_archives": len(archives)
    }


@router.get("/files/archive")
async def list_archive(
    path: str = Query(..., description="Path to archive file")
) -> dict:
    """List convertible files inside an archive."""
    if not validate_path(path):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Archive not found")

    if not archive_service.is_archive(path):
        raise HTTPException(status_code=400, detail="Not a supported archive format")

    contents = archive_service.list_archive_contents(path)

    return {
        "archive": path,
        "files": contents,
        "total": len(contents)
    }
