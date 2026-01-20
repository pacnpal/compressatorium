import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.models import CHDInfo
from app.services.chdman import chdman_service

router = APIRouter()


def validate_path(path: str) -> bool:
    """Validate that a path is within configured volumes."""
    real_path = os.path.realpath(path)
    for volume in settings.volumes:
        real_volume = os.path.realpath(volume)
        if real_path.startswith(real_volume + os.sep) or real_path == real_volume:
            return True
    return False


@router.get("/info", response_model=CHDInfo)
async def get_chd_info(
    path: str = Query(..., description="Path to CHD file")
):
    """Get information about a CHD file."""
    if not validate_path(path):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        info = await chdman_service.info(path)

        return CHDInfo(
            file=path,
            input_file=info.get("input_file"),
            file_version=info.get("file_version"),
            logical_size=info.get("logical_size"),
            hunk_size=info.get("hunk_size"),
            total_hunks=info.get("total_hunks"),
            unit_size=info.get("unit_size"),
            total_units=info.get("total_units"),
            compression=info.get("compression"),
            chd_size=info.get("chd_size"),
            ratio=info.get("ratio"),
            sha1=info.get("sha1"),
            data_sha1=info.get("data_sha1"),
            raw_data=info.get("raw_data", "")
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read CHD info: {str(e)}")
