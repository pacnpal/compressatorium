"""What a Rules editor reads and does, for any Library.

The Library routes (``/api/libraries/{id}/...``, RomM's id is ``romm``) are
thin wrappers over these, so the payload, the policy check and the sweep calls
exist once.
"""

from __future__ import annotations

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


async def forget_converted(library: Library, platform_ids: list | None) -> dict[str, Any]:
    """Clear the converted history (and failed holds) for *platform_ids*, or all.

    ``None`` means every Platform; an empty list means none.
    """
    cleared = await rules_sweep.forget_converted(
        library, [str(p) for p in platform_ids] if platform_ids is not None else None,
    )
    return {"cleared": cleared, "state": await rules_sweep.get_state(library)}


async def sweep_now(
    library: Library, *, platform_ids: list | None, limit: int | None, dry_run: bool,
) -> dict:
    """Preview (*dry_run*) or run a sweep now, ignoring the schedule clock."""
    return await rules_sweep.sweep(
        library, platform_ids=platform_ids, ignore_schedule=True,
        dry_run=dry_run, overall_limit=limit,
    )
