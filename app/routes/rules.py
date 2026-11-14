"""Rule routes shared by every Library: the notices a rule editor shows once (#302)."""

from fastapi import APIRouter
from services.rules import store

router = APIRouter()


@router.get("/rules/notices")
async def get_rule_notices() -> dict:
    """Which one-time notices the rule editor should show.

    ``check_level``: the upgrade moved delete-source Rules to the Round-trip
    check. ``show`` until dismissed, and only when it switched a Rule.
    """
    return {"check_level": await store.check_level_notice()}


@router.post("/rules/notices/check-level/dismiss")
async def dismiss_check_level_notice() -> dict:
    """Stop showing the check-level notice. Stored, so it stays dismissed."""
    return {"check_level": await store.dismiss_check_level_notice()}
