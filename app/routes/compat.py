"""Compatibility presets route (#429): the "Compatible with..." table."""

from fastapi import APIRouter
from services import compat_presets

router = APIRouter()


@router.get("/compat-presets")
async def get_compat_presets() -> dict:
    """The targets and the modes and codecs each reads, every entry sourced."""
    return compat_presets.served_presets()
