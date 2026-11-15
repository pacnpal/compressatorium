"""Notification routes (#293): destinations and the Test button.

A saved destination URL is a secret and is never returned; the browser sees an
id, a kind, a label and a harmless hint (a webhook's host, an Apprise scheme).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from services import notifications

router = APIRouter()


class NewDestination(BaseModel):
    kind: str = Field(max_length=32)  # one of notifications.KINDS, checked on save
    url: str = Field(max_length=notifications.MAX_URL_LEN)
    label: str | None = Field(default=None, max_length=notifications.MAX_LABEL_LEN)


class DestinationEdit(BaseModel):
    # Omit url to keep the stored one: it is never sent to the browser to edit.
    url: str | None = Field(default=None, max_length=notifications.MAX_URL_LEN)
    label: str | None = Field(default=None, max_length=notifications.MAX_LABEL_LEN)


class DraftTest(BaseModel):
    kind: str = Field(max_length=32)
    url: str = Field(max_length=notifications.MAX_URL_LEN)


def _refuse(exc: notifications.NotificationError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/notifications")
async def get_notifications() -> dict:
    return {
        "destinations": await notifications.list_destinations(),
        "events": notifications.event_list(),
        "kinds": notifications.kind_list(),
        "apprise_available": notifications.apprise_available(),
    }


@router.post("/notifications/destinations")
async def add_destination(body: NewDestination) -> dict:
    try:
        return await notifications.add_destination(body.kind, body.url, body.label)
    except notifications.NotificationError as exc:
        raise _refuse(exc) from None


@router.put("/notifications/destinations/{dest_id}")
async def edit_destination(dest_id: str, body: DestinationEdit) -> dict:
    try:
        dest = await notifications.update_destination(dest_id, body.url, body.label)
    except notifications.NotificationError as exc:
        raise _refuse(exc) from None
    if dest is None:
        raise HTTPException(status_code=404, detail="No such destination")
    return dest


@router.delete("/notifications/destinations/{dest_id}")
async def delete_destination(dest_id: str) -> dict:
    if not await notifications.remove_destination(dest_id):
        raise HTTPException(status_code=404, detail="No such destination")
    return {"ok": True}


@router.post("/notifications/destinations/{dest_id}/test")
async def test_saved_destination(dest_id: str) -> dict:
    result = await notifications.test_destination(dest_id=dest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No such destination")
    return result


@router.post("/notifications/test")
async def test_draft(body: DraftTest) -> dict:
    """Test a URL before saving it."""
    return await notifications.test_destination(kind=body.kind, url=body.url)
