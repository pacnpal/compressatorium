"""The Patch action's routes (#428): upload a patch, start a patch job.

The work is in ``services.patch_bench``. Nothing here changes an existing
file: the job writes a new ``Name (Patched).7z`` beside the Image.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from services import patch_bench, upload_stream

router = APIRouter()


class PatchRequest(BaseModel):
    # A loose ROM, a .zip/.7z holding one, or "archive.zip::member".
    image_path: str
    # Exactly one of these: a patch file in a Volume, or an upload's id.
    patch_path: str | None = None
    upload_id: str | None = None


# The body is read by ``services.upload_stream``, not FastAPI's ``UploadFile``
# (which spools it whole first), so this documents the form it takes.
_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}},
        }}},
    },
}


@router.post("/patch/upload", openapi_extra=_UPLOAD_BODY)
async def upload_patch(request: Request) -> dict:
    """Keep an IPS or BPS patch (the multipart ``file``) in the app's data
    directory; its ``upload_id`` names it in ``POST /patch``.

    Refused (413) over ``CHD_PATCH_MAX_PATCH_SIZE`` while it arrives: by its
    ``Content-Length`` before a byte is read, or the moment a body sent without
    one passes the limit. Nothing oversized is spooled or kept.
    """
    try:
        upload = await upload_stream.file_upload(
            request, field="file", max_bytes=patch_bench.max_patch_size(),
        )
        try:
            return await patch_bench.save_upload(upload.filename, upload.chunks)
        finally:
            await upload.chunks.aclose()
    except (patch_bench.PatchRefused, upload_stream.UploadRefused) as exc:
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
