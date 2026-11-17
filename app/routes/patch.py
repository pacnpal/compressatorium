"""The Patch action's routes (#428): upload a patch, start a patch job.

The work is in ``services.patch_bench``. Nothing here changes an existing
file: the job writes a new ``Name (Patched).7z`` beside the Image.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from services import patch_bench

router = APIRouter()


class PatchRequest(BaseModel):
    # A loose ROM, a .zip/.7z holding one, or "archive.zip::member".
    image_path: str
    # Exactly one of these: a patch file in a Volume, or an upload's id.
    patch_path: str | None = None
    upload_id: str | None = None


@router.post("/patch/upload")
async def upload_patch(file: Annotated[UploadFile, File()]) -> dict:
    """Keep an IPS or BPS patch in the app's data directory; its ``upload_id``
    names it in ``POST /patch``. Refused over ``CHD_PATCH_MAX_PATCH_SIZE``."""
    try:
        return await patch_bench.save_upload(file.filename or "", file.read)
    except patch_bench.PatchRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@router.post("/patch")
async def start_patch(request: PatchRequest) -> dict:
    """Patch one cartridge Image as a background job; answers ``{job_id}``.

    Refused (400) for a disc Image, an Image or patch outside the Volumes, a
    patch that isn't ``.ips``/``.bps``, or a request naming no patch or two.
    """
    try:
        return await patch_bench.start(
            request.image_path, patch_path=request.patch_path, upload_id=request.upload_id,
        )
    except patch_bench.PatchRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
