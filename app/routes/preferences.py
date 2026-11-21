"""Preferences routes: server-stored UI layout widths and compression defaults.

Single-user app, no auth. The workspace layout (panel split + per-tool
table column widths) lives under the ``layout`` preference key so it
survives a browser/cache wipe.
"""

from fastapi import APIRouter
from logging_setup import get_logger
from models import ConversionPreferences, DefaultsPreferences, LayoutPreferences
from pydantic import ValidationError
from services.preferences_store import preferences_store

logger = get_logger("routes.preferences")

router = APIRouter()

# The single key the workspace layout is stored under.
LAYOUT_KEY = "layout"
# Per-tool compression defaults (the convert panel's remembered settings).
CONVERSION_KEY = "conversion"
# The safety choices new conversions and new console settings start with.
DEFAULTS_KEY = "defaults"


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


@router.get("/preferences/defaults")
async def get_defaults_preferences() -> dict:
    """Return the stored defaults, filled from the model where unset."""
    stored = await preferences_store.get(DEFAULTS_KEY)
    if not isinstance(stored, dict):
        stored = {}
    known = {k: v for k, v in stored.items() if k in DefaultsPreferences.model_fields}
    try:
        return DefaultsPreferences.model_validate(known).model_dump()
    except ValidationError:
        logger.warning("Stored defaults are unreadable; answering the model's defaults")
        return DefaultsPreferences().model_dump()


@router.put("/preferences/defaults")
async def put_defaults_preferences(prefs: DefaultsPreferences) -> dict:
    """Store the defaults and return the saved object."""
    return await preferences_store.put(DEFAULTS_KEY, prefs.model_dump())
