import os

from fastapi import APIRouter, HTTPException, Query

from models import CHDInfo
from services.chdman import chdman_service
from services.verification_store import verification_store
from utils.path_utils import is_within_configured_volumes

router = APIRouter()


@router.get("/info", response_model=CHDInfo)
async def get_chd_info(
    path: str = Query(..., description="Path to CHD file")
):
    """Get information about a CHD file."""
    if not is_within_configured_volumes(path, treat_archives=False):
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


@router.get("/verify")
async def verify_chd(
    path: str = Query(..., description="Path to CHD file to verify")
) -> dict:
    """Verify the integrity of a CHD file."""
    if not is_within_configured_volumes(path, treat_archives=False):
        raise HTTPException(status_code=403, detail="Access denied: path outside configured volumes")

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if not path.lower().endswith(".chd"):
        raise HTTPException(status_code=400, detail="Not a CHD file")

    try:
        result = await chdman_service.verify(path)
        if result.get("valid"):
            verification_store.mark_verified(path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify CHD: {str(e)}")


@router.get("/verified")
async def list_verified() -> dict:
    """List verified CHD paths."""
    verification_store.prune_missing()
    verified = []
    for record in verification_store.all_records():
        chd_path = record.get("chd_path")
        if chd_path and is_within_configured_volumes(chd_path, treat_archives=False):
            verified.append(chd_path)
    return {"verified": verified}
