"""Preferences routes — server-stored UI layout widths.

Single-user app, no auth. The workspace layout (panel split + per-tool
table column widths) lives under the ``layout`` preference key so it
survives a browser/cache wipe.
"""

import logging

from fastapi import APIRouter
from models import LayoutPreferences
from services.preferences_store import preferences_store

logger = logging.getLogger("chd.routes.preferences")

router = APIRouter()

# The single key the workspace layout is stored under.
LAYOUT_KEY = "layout"


@router.get("/preferences")
async def get_preferences() -> dict:
    """Return the stored workspace layout, or an empty object if unset."""
    stored = await preferences_store.get(LAYOUT_KEY)
    return stored or {}


@router.put("/preferences")
async def put_preferences(prefs: LayoutPreferences) -> dict:
    """Upsert the workspace layout and return the saved object."""
    payload = prefs.model_dump(exclude_none=True)
    return await preferences_store.put(LAYOUT_KEY, payload)
