"""Whether a path may be renamed, deleted or replaced right now (#306).

One rule for every guard that is about to mutate a path (the web's rename and
delete, the job's delete check): the path's own cross-process lock, and, for a
numbered split part (``Game.iso.0``), the lock on its logical owner
(``Game.iso``). A split build locks only the logical output and writes its
parts as it goes, so a part has no lock file of its own; the owner is found by
name (``registry.split_owner_name``), so a part made after the lock was taken
is covered too.
"""
from __future__ import annotations

from collections.abc import Iterable

from services.lock_manager import lock_manager
from services.tools import registry


def locked_for_mutation(path: str, *, ignore: Iterable[str] = ()) -> str | None:
    """The locked path that refuses mutating *path*: *path* itself or its
    split owner. None when neither is locked. Paths in *ignore* (locks the
    caller holds itself) are not probed. May do disk I/O."""
    skip = set(ignore)
    for candidate in (path, registry.split_owner_name(path)):
        if candidate and candidate not in skip and lock_manager.is_locked(candidate):
            return candidate
    return None
