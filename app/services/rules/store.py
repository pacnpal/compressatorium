"""Where Rules and their run state are stored: one row each, keys prefixed by Library.

Two preference rows hold every Library's data: ``rules`` (the operator's
configuration) and ``rules.state`` (schedule stamps and converted history).
They stay separate on purpose: mixing "when did this last run" into the rule
blob means every sweep rewrites the operator's configuration.

Inside each row a Rule's key is ``f"{library.id}:{platform_id}"``, so a Rule
belongs to one (Library, Platform) pair. Callers never see the prefix: they
read and write one Library's view, keyed by bare Platform id.

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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from services.preferences_store import preferences_store

if TYPE_CHECKING:
    from services.rules.library import Library

RULES_KEY = "rules"
STATE_KEY = "rules.state"
# ``{library id: when its legacy rows were copied}``. Written last, so a crash
# part way through the copy repeats it from the same unchanged rows.
MIGRATED_KEY = "rules.migrated"

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
    for kind, legacy_key in legacy_keys.items():
        legacy = await preferences_store.get(legacy_key)
        if not isinstance(legacy, dict) or not legacy:
            continue
        blob = await preferences_store.get(kind)
        blob = dict(blob) if isinstance(blob, dict) else {}
        for platform_id, value in legacy.items():
            blob[f"{prefix}{platform_id}"] = value
        await preferences_store.put(kind, blob)
    migrated[library.id] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    await preferences_store.put(MIGRATED_KEY, migrated)


async def _load(kind: str, library: Library) -> dict:
    await _copy_legacy(library)
    blob = await preferences_store.get(kind)
    return dict(blob) if isinstance(blob, dict) else {}


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
