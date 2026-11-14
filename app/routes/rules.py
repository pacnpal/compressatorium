"""Rule routes shared by every Library: the notices a rule editor shows once (#302)."""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from services import libraries
from services.rules import store

router = APIRouter()


async def _libraries() -> list:
    """The app's Libraries, so their legacy Rule rows are copied before a notice is read.

    RomM and every registered folder Library (#303).
    """
    return await run_in_threadpool(libraries.all_libraries)


@router.get("/rules/notices")
async def get_rule_notices() -> dict:
    """Which one-time notices the rule editor should show.

    ``check_level``: the upgrade moved delete-source Rules to the Round-trip
    check. ``show`` until dismissed, and only when a switched Rule's mode can
    Round-trip. ``container_check`` counts switched Rules whose mode cannot yet.
    """
    return {"check_level": await store.check_level_notice(await _libraries())}


@router.post("/rules/notices/check-level/dismiss")
async def dismiss_check_level_notice() -> dict:
    """Stop showing the check-level notice. Stored, so it stays dismissed."""
    return {"check_level": await store.dismiss_check_level_notice(await _libraries())}
