"""Preferences routes: server-stored UI layout widths and compression defaults.

Single-user app, no auth. The workspace layout (panel split + per-tool
table column widths) lives under the ``layout`` preference key so it
survives a browser/cache wipe.
"""

from logging_setup import get_logger

from fastapi import APIRouter
from models import ConversionPreferences, LayoutPreferences
from services.preferences_store import preferences_store

logger = get_logger("routes.preferences")

router = APIRouter()

# The single key the workspace layout is stored under.
LAYOUT_KEY = "layout"
# Per-tool compression defaults (the convert panel's remembered settings).
CONVERSION_KEY = "conversion"


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


@router.get("/preferences/conversion")
async def get_conversion_preferences() -> dict:
    """Return remembered per-tool compression settings, or an empty object."""
    stored = await preferences_store.get(CONVERSION_KEY)
    return stored or {}


@router.put("/preferences/conversion")
async def put_conversion_preferences(prefs: ConversionPreferences) -> dict:
    """Upsert per-tool compression settings and return the saved object."""
    return await preferences_store.put(CONVERSION_KEY, prefs.model_dump(exclude_none=True))
