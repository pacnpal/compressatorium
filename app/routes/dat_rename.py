"""Rename Images to their DAT name (#425): the preview and the confirmed run.

``POST /api/files/dat-rename/preview`` lists, for each Image, the old and new
path and every Referrer update, or why it is skipped; it changes nothing.
``POST /api/files/dat-rename`` takes the preview as confirmed, with the
rename confirmation header, and starts the rename as a background job
(``{job_id}``), like the duplicate delete. The single-file rename route
(``POST /api/files/rename``) is a separate, unchanged action.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from routes.files import CONFIRM_RENAME_FILE, _require_action_confirmation
from services import dat_rename

router = APIRouter()


class DatRenamePreviewRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=dat_rename.MAX_IMAGES)


class DatRenameFile(BaseModel):
    path: str
    new_path: str | None = None


class DatRenameItem(BaseModel):
    """One Image as the preview showed it: its new path, or None when skipped,
    and, for an Image of several files (#426), each file's new path. A
    re-plan that differs from what was confirmed skips the Image."""

    path: str
    new_path: str | None = None
    files: list[DatRenameFile] | None = Field(default=None, max_length=1000)


class DatRenameRunRequest(BaseModel):
    items: list[DatRenameItem] = Field(min_length=1, max_length=dat_rename.MAX_IMAGES)


@router.post("/files/dat-rename/preview")
async def dat_rename_preview(payload: DatRenamePreviewRequest) -> dict:
    """What renaming each Image to its DAT name would change. Changes nothing."""
    return await dat_rename.preview(payload.paths)


@router.post("/files/dat-rename")
async def dat_rename_run(payload: DatRenameRunRequest, request: Request) -> dict:
    """Rename the Images of a confirmed preview, as a background job."""
    _require_action_confirmation(request, CONFIRM_RENAME_FILE, "rename action")
    try:
        return await dat_rename.start_run([item.model_dump() for item in payload.items])
    except dat_rename.RenameRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
