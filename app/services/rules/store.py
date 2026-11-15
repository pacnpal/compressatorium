"""Where Rules and their run state are stored: one row each, keys prefixed by Library.

Two preference rows hold every Library's data: ``rules`` (the operator's
configuration) and ``rules.state`` (schedule stamps and converted history).
They stay separate on purpose: mixing "when did this last run" into the rule
blob means every sweep rewrites the operator's configuration.

Inside each row a Rule's key is ``f"{library.id}:{platform_id}"``. Check Rules
(#305) have rows of their own, ``rules.check`` and ``rules.check.state``, so a
Rule's identity is (Library, Platform, kind) and a Platform can have a
conversion Rule and a check Rule. Callers never see the prefix: they read and
write one Library's view of one row, keyed by bare Platform id.

Before #296 RomM kept its Rules unprefixed under ``romm.rules`` and
``romm.auto_state``. A Library names such rows in ``legacy_preference_keys``.
The first read or write copies their entries under the prefixed keys and then
records the Library in the ``rules.migrated`` row, so the copy happens once.
The old rows are never written or deleted: they stay as a copy of the Rules
and history at upgrade time, which an earlier release can still read after a
downgrade. Anything written to them later is ignored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from services.preferences_store import preferences_store
from services.rules.rules import CHECK_LEVELS
from services.tools import registry

if TYPE_CHECKING:
    from services.rules.library import Library

RULES_KEY = "rules"
STATE_KEY = "rules.state"
# The check Rules and their run state (#305). Kept apart from the conversion
# rows, which an earlier release reads and the legacy copy and the check-level
# upgrade walk.
CHECK_RULES_KEY = "rules.check"
CHECK_STATE_KEY = "rules.check.state"
# Rules row, state row, by Rule kind.
_KIND_KEYS = {
    "convert": (RULES_KEY, STATE_KEY),
    "check": (CHECK_RULES_KEY, CHECK_STATE_KEY),
}


def rules_key(kind: str) -> str:
    """The row a Rule *kind* is stored in."""
    return _KIND_KEYS[kind][0]


def state_key(kind: str) -> str:
    """The row a Rule *kind*'s run state is stored in."""
    return _KIND_KEYS[kind][1]
# ``{library id: when its legacy rows were copied}``. Written last, so a crash
# part way through the copy repeats it from the same unchanged rows.
MIGRATED_KEY = "rules.migrated"

# ``{"upgraded_at": ..., "switched": [full keys]}``: the one-time move of every
# stored delete-source Rule to the Round-trip check (#302). Like
# ``MIGRATED_KEY`` it is written last, so a crash part way repeats the pass,
# which sets the same values again.
CHECK_LEVEL_UPGRADE_KEY = "rules.check_level_upgrade"
# ``{notice id: when it was dismissed}``.
NOTICES_KEY = "rules.notices"

_SEPARATOR = ":"

# Serialises each read-modify-write of the shared rows. The sweep lock already
# covers every write the engine makes, but reads come from the API outside it,
# and the first of those may be the one that copies the legacy rows.
_lock = asyncio.Lock()


def _prefix(library: Library) -> str:
    return f"{library.id}{_SEPARATOR}"


def _view(blob: dict, library: Library) -> dict:
    """*library*'s entries of *blob*, keyed by bare Platform id."""
    prefix = _prefix(library)
    out = {}
    for key, value in blob.items():
        if not key.startswith(prefix):
            continue
        platform_id = key[len(prefix):]
        # A Platform id never holds the separator, so this key belongs to
        # another Library whose id starts with this one's ("folder:/a" and
        # "folder:/a:b").
        if _SEPARATOR in platform_id:
            continue
        out[platform_id] = value
    return out


async def _copy_legacy(library: Library) -> None:
    """Copy *library*'s pre-#296 rows under the prefixed keys, once.

    Called with ``_lock`` held, before every read and write, so nothing is
    written to the prefixed keys before the copy is done.
    """
    legacy_keys = library.legacy_preference_keys or {}
    if not legacy_keys:
        return
    migrated = await preferences_store.get(MIGRATED_KEY)
    migrated = dict(migrated) if isinstance(migrated, dict) else {}
    if library.id in migrated:
        return
    prefix = _prefix(library)
    switched: list[str] = []
    for kind, legacy_key in legacy_keys.items():
        legacy = await preferences_store.get(legacy_key)
        if not isinstance(legacy, dict) or not legacy:
            continue
        blob = await preferences_store.get(kind)
        blob = dict(blob) if isinstance(blob, dict) else {}
        for platform_id, value in legacy.items():
            blob[f"{prefix}{platform_id}"] = value
        if kind == RULES_KEY:
            # These rows predate the check level. If the upgrade pass already
            # ran (another Library was read first) it did not see them.
            switched = _switch_to_round_trip(blob, [f"{prefix}{p}" for p in legacy])
        await preferences_store.put(kind, blob)
    upgrade = await preferences_store.get(CHECK_LEVEL_UPGRADE_KEY)
    if switched and isinstance(upgrade, dict):
        upgrade = {**upgrade, "switched": sorted({*upgrade.get("switched", []), *switched})}
        await preferences_store.put(CHECK_LEVEL_UPGRADE_KEY, upgrade)
    migrated[library.id] = _now()
    await preferences_store.put(MIGRATED_KEY, migrated)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _switch_to_round_trip(blob: dict, keys) -> list[str]:
    """Set every delete-source Rule among *keys* of *blob* to Round-trip, in place.

    Returns the keys it set. A Rule that keeps its Source is left as it was.
    """
    switched = []
    for key in keys:
        rule = blob.get(key)
        if isinstance(rule, dict) and rule.get("delete_on_verify"):
            blob[key] = {**rule, "check_level": CHECK_LEVELS[0]}
            switched.append(key)
    return sorted(switched)


async def _upgrade_check_level() -> dict:
    """Move every stored delete-source Rule to the Round-trip check, once (#302).

    Every Library's Rules, in one pass over the shared row. Called with
    ``_lock`` held, after the Library's legacy copy and before any read or
    write, so nothing an operator saves afterwards is overwritten. Returns
    the marker.
    """
    upgrade = await preferences_store.get(CHECK_LEVEL_UPGRADE_KEY)
    if isinstance(upgrade, dict):
        return upgrade
    blob = await preferences_store.get(RULES_KEY)
    blob = dict(blob) if isinstance(blob, dict) else {}
    switched = _switch_to_round_trip(blob, list(blob))
    if switched:
        await preferences_store.put(RULES_KEY, blob)
    upgrade = {"upgraded_at": _now(), "switched": switched}
    await preferences_store.put(CHECK_LEVEL_UPGRADE_KEY, upgrade)
    return upgrade


async def _load(kind: str, library: Library) -> dict:
    await _copy_legacy(library)
    await _upgrade_check_level()
    blob = await preferences_store.get(kind)
    return dict(blob) if isinstance(blob, dict) else {}


async def check_level_notice(libraries: Iterable[Library] = ()) -> dict:
    """The one-time notice about the upgrade: ``{"show", "switched", "container_check"}``.

    *libraries* are the app's Libraries. Their legacy copies run first, so the
    count includes Rules an earlier release stored under old keys even when
    the editor asks before anything has read them.

    ``switched`` counts the switched Rules that now Round-trip (their mode has
    a digest, read from the registry); ``container_check`` the switched Rules
    whose mode has none, which keep the container check for now. Shown only
    when ``switched`` is not zero and nobody has dismissed it.
    """
    async with _lock:
        for library in libraries:
            await _copy_legacy(library)
        upgrade = await _upgrade_check_level()
        rules = await preferences_store.get(RULES_KEY)
        notices = await preferences_store.get(NOTICES_KEY)
    rules = rules if isinstance(rules, dict) else {}
    switched = container_check = 0
    for key in upgrade.get("switched") or []:
        rule = rules.get(key)
        if not isinstance(rule, dict):
            continue
        if registry.mode_supports_round_trip(str(rule.get("mode") or "")):
            switched += 1
        else:
            container_check += 1
    dismissed = isinstance(notices, dict) and bool(notices.get("check_level"))
    return {
        "show": bool(switched) and not dismissed,
        "switched": switched,
        "container_check": container_check,
    }


async def dismiss_check_level_notice(libraries: Iterable[Library] = ()) -> dict:
    """Remember that the notice was dismissed. Returns the notice as it now stands."""
    async with _lock:
        notices = await preferences_store.get(NOTICES_KEY)
        notices = dict(notices) if isinstance(notices, dict) else {}
        notices.setdefault("check_level", _now())
        await preferences_store.put(NOTICES_KEY, notices)
    return await check_level_notice(libraries)


async def read(kind: str, library: Library) -> dict:
    """*library*'s Rules or state, keyed by bare Platform id."""
    async with _lock:
        return _view(await _load(kind, library), library)


async def write(kind: str, library: Library, entries: dict) -> None:
    """Replace *library*'s Rules or state with *entries*. Other Libraries keep theirs."""
    prefix = _prefix(library)
    async with _lock:
        blob = await _load(kind, library)
        blob = {k: v for k, v in blob.items() if k not in _keys(blob, library)}
        for platform_id, value in entries.items():
            platform_id = str(platform_id)
            if _SEPARATOR in platform_id:
                raise ValueError(f"a Platform id cannot contain {_SEPARATOR!r}")
            blob[f"{prefix}{platform_id}"] = value
        await preferences_store.put(kind, blob)


def _keys(blob: dict, library: Library) -> set[str]:
    return {f"{_prefix(library)}{pid}" for pid in _view(blob, library)}
