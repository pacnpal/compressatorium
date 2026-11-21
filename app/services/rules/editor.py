"""What a Rules editor reads and does, for any Library.

RomM's routes (``/api/romm/rules``, ``/api/romm/auto-convert/*``) and the
Library routes (``/api/libraries/{id}/...``) are both thin wrappers over these,
so the payload, the policy check and the sweep calls exist once.
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.rules import rules as schema
from services.rules import sweep as rules_sweep
from services.rules.library import Library, duplicate_actions
from services.rules.rules import CHECK


async def editor_payload(library: Library) -> dict[str, Any]:
    """*library*'s Rules and run state, with the schema the editor renders from.

    ``rules`` / ``state`` are the conversion Rules, ``check_rules`` /
    ``check_state`` the check Rules (#305), each keyed by Platform id: a
    Platform can have one of each.

    ``defaults`` and ``options`` ship with the Rules so the UI never carries a
    second copy of what a valid Rule looks like. ``options.duplicate_actions``
    is what the Library can honour (``skip`` only without converted history).
    """
    return {
        "rules": await rules_sweep.get_rules(library),
        "state": await rules_sweep.get_state(library),
        "check_rules": await rules_sweep.get_rules(library, CHECK),
        "check_state": await rules_sweep.get_state(library, CHECK),
        "defaults": schema.default_rule(defaults=library.rule_defaults()),
        "options": {
            "orders": list(schema.ORDERS),
            "duplicate_actions": list(duplicate_actions(library)),
            "days": list(schema.ALL_DAYS),
        },
    }


async def save_rules(library: Library, payload: Any) -> dict[str, Any]:
    """Replace *library*'s Rules from a submission (``{"rules": ...}`` or the bare map).

    Strict: a Rule asking for a policy the Library cannot honour raises
    :class:`~services.rules.library.UnsupportedPolicy` and nothing is saved.
    ``check_rules`` replaces the check Rules when the submission has it; a
    submission without it (an older editor, a bare map) leaves them as they are,
    and one with only ``check_rules`` leaves the conversion Rules.
    """
    as_dict = isinstance(payload, dict)
    has_checks = as_dict and "check_rules" in payload
    only_checks = has_checks and "rules" not in payload
    out: dict[str, Any] = {}
    if only_checks:
        out["rules"] = await rules_sweep.get_rules(library)
    else:
        raw = payload.get("rules", payload) if as_dict else payload
        out["rules"] = await rules_sweep.set_rules(library, raw, strict=True)
    if has_checks:
        out["check_rules"] = await rules_sweep.set_check_rules(library, payload["check_rules"])
    else:
        out["check_rules"] = await rules_sweep.get_rules(library, CHECK)
    return out


# One merge at a time: two patches each reading the stored Rules before the
# other wrote would each write back without the other's console.
_patch_lock = asyncio.Lock()


def _merged(stored: dict, patch: Any) -> dict:
    """*stored* with each console *patch* names replaced (``None`` removes it)."""
    out = dict(stored)
    for platform_id, rule in (patch or {}).items():
        if rule is None:
            out.pop(str(platform_id), None)
        else:
            out[str(platform_id)] = rule
    return out


async def patch_rules(library: Library, payload: Any) -> dict[str, Any]:
    """Merge the consoles a submission names into *library*'s stored Rules
    (``{"rules": {id: rule | null}, "check_rules": {id: rule | null}}``) and
    save the result through :func:`save_rules`, with all its checks.

    What the console table sends (#541, Codex 4653298940): one console's
    change, never the whole map, so a table holding none or some of the
    Rules (one that saved before they loaded) can't delete the others.
    """
    if not isinstance(payload, dict):
        payload = {}
    async with _patch_lock:
        merged: dict[str, Any] = {
            "rules": _merged(await rules_sweep.get_rules(library), payload.get("rules")),
        }
        if "check_rules" in payload:
            merged["check_rules"] = _merged(
                await rules_sweep.get_rules(library, CHECK), payload.get("check_rules"),
            )
        return await save_rules(library, merged)


async def forget_converted(
    library: Library, platform_ids: list | None, *, failed_only: bool = False,
) -> dict[str, Any]:
    """Clear the converted history (and failed holds) for *platform_ids*, or all.

    ``None`` means every Platform; an empty list means none. *failed_only*
    releases the failed holds alone and keeps the converted history (#541).
    """
    ids = [str(p) for p in platform_ids] if platform_ids is not None else None
    if failed_only:
        cleared = await rules_sweep.release_failed(library, ids)
    else:
        cleared = await rules_sweep.forget_converted(library, ids)
    return {"cleared": cleared, "state": await rules_sweep.get_state(library)}


async def sweep_now(
    library: Library, *, platform_ids: list | None, limit: int | None, dry_run: bool,
) -> dict:
    """Preview (*dry_run*) or run a sweep now, ignoring the schedule clock."""
    return await rules_sweep.sweep(
        library, platform_ids=platform_ids, ignore_schedule=True,
        dry_run=dry_run, overall_limit=limit,
    )
