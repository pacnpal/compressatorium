"""RomM's Rules: the shared Rule engine, bound to the RomM Library.

The engine itself lives in :mod:`services.rules` (schema and validation in
``rules``, the clock in ``schedule``, everything stateful in ``sweep``). This
module is what the RomM routes and startup call, with the same names and
arguments they used before the engine moved out of this package, so the API
did not change. Each function passes :data:`services.romm.library.romm_library`.
"""

from __future__ import annotations

from typing import Any

from services.romm.library import romm_library
from services.rules import rules as _rules
from services.rules import sweep as _sweep

ORDERS = _rules.ORDERS
DUPLICATE_ACTIONS = _rules.DUPLICATE_ACTIONS
ALL_DAYS = _rules.ALL_DAYS

# Process-wide, not per Library: hold off every sweep.
paused = _sweep.paused
# Registered with the job manager at startup. Finds the Library from the job.
note_job_finished = _sweep.note_job_finished


def default_rule(mode: str = "") -> dict[str, Any]:
    return _rules.default_rule(mode, defaults=romm_library.rule_defaults())


def normalize_rule(raw: Any, **kwargs) -> dict[str, Any] | None:
    return _rules.normalize_rule(raw, defaults=romm_library.rule_defaults(), **kwargs)


def normalize_rules(raw: Any, **kwargs) -> dict[str, dict]:
    return _sweep._normalize(romm_library, raw, **kwargs)


async def get_rules() -> dict[str, dict]:
    return await _sweep.get_rules(romm_library)


async def set_rules(raw: Any) -> dict[str, dict]:
    return await _sweep.set_rules(romm_library, raw)


async def get_state() -> dict[str, dict]:
    return await _sweep.get_state(romm_library)


async def forget_converted(platform_ids: list[str] | None = None) -> int:
    return await _sweep.forget_converted(romm_library, platform_ids)


async def forget_converted_locked(
    platform_ids: list[str] | None = None, *, owner: str | None = None,
) -> int:
    return await _sweep.forget_converted_locked(romm_library, platform_ids, owner=owner)


async def sweep(**kwargs) -> dict:
    return await _sweep.sweep(romm_library, **kwargs)


async def run_forever() -> None:
    """The scheduler over every Library (``services.libraries.run_forever``).

    Kept for callers of the old name; ``main.py`` starts
    ``services.libraries.run_forever`` itself. ``compressatorium run-rules``
    sweeps the same ``services.libraries.all_libraries()`` list.
    """
    from services import libraries

    await libraries.run_forever()
