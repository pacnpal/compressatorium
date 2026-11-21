"""Rule routes shared by every Library.

The notices a rule editor shows once (#302), and the helpers RomM's Rule routes
and the Library routes (#303) both use, so the two stay one implementation.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from services import libraries
from services.rules import editor, store
from services.rules.library import Library, UnsupportedPolicy
from services.rules.rules import UnusableCompression, pattern_problem

router = APIRouter()


async def save_rules(library: Library, payload: Any) -> dict:
    """``PUT .../rules`` for any Library: 422 for a policy the Library cannot
    honour, 400 for a codec the Rule's mode can't use (the job route's answer
    for the same pair, #474)."""
    try:
        return await editor.save_rules(library, payload)
    except UnsupportedPolicy as exc:
        raise HTTPException(status_code=422, detail=exc.detail()) from None
    except UnusableCompression as exc:
        raise HTTPException(status_code=400, detail=exc.problem) from None


def requested_platform_ids(payload: dict | None) -> list[str] | None:
    """The ``platform_ids`` a forget-converted request names, as strings.

    Absent means every Platform; an explicit empty list means none. Reading
    ``[]`` as "no filter" would wipe the history for every Platform on an
    empty selection, and the next sweep would reconvert the whole Library.
    """
    ids = (payload or {}).get("platform_ids")
    return [str(p) for p in ids] if ids is not None else None


async def forget_converted(library: Library, payload: dict | None) -> dict:
    """``POST .../rules/forget-converted`` for any Library."""
    return await editor.forget_converted(library, requested_platform_ids(payload))


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


@router.post("/rules/check-pattern")
async def check_pattern(payload: dict) -> dict:
    """Whether ``pattern`` can be a Rule's name filter: the check saving one
    runs (`rules.pattern_problem`), asked first so the console table never
    autosaves a filter the save would refuse and pause the console for
    (#541). Pure: compiles a string of at most 500 characters."""
    problem = pattern_problem(payload.get("pattern"))
    return {"ok": problem is None, "error": problem}
