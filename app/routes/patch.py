"""Patch bench routes (#428): upload a patch file, start a patch job."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from routes.volume_guard import off_loop_bounded
from services import patch_bench

from config import settings

router = APIRouter()


class PatchRequest(BaseModel):
    image_path: str
    patch_path: str


@router.post("/patch")
async def start_patch(body: PatchRequest) -> dict:
    """Start patching one Image; ``{job_id}``. The job writes ``Name (Patched).7z``."""
    try:
        plan = await off_loop_bounded(
            patch_bench.check_request, body.image_path, body.patch_path,
        )
    except patch_bench.PatchRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read the Image: {exc}") from None
    return await patch_bench.start(plan)


def _open_upload(name: str):
    folder = os.path.join(patch_bench.uploads_dir(), uuid.uuid4().hex[:12])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    return path, open(path, "xb")


@router.post("/patch/upload")
async def upload_patch(file: UploadFile = File(...)) -> dict:
    """Keep an uploaded ``.ips``/``.bps`` in ``<data_dir>/patches``; ``{path}``."""
    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(patch_bench.PATCH_EXTENSIONS) or name.startswith("."):
        raise HTTPException(status_code=400, detail="The patch must be an .ips or .bps file")
    path, out = await run_in_threadpool(_open_upload, name)
    total = 0
    try:
        with out:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > settings.patch_max_size:
                    raise HTTPException(status_code=400, detail=patch_bench.too_big())
                await run_in_threadpool(out.write, chunk)
    except BaseException:
        await run_in_threadpool(os.unlink, path)
        raise
    return {"path": path, "size": total}
