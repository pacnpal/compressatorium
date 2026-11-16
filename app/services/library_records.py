"""What a Library's records are, forgotten in one call (#388).

A Library's Findings (with their dismissals), its last find-duplicates run and
its check times are all keyed by the Library's id and its Image keys. They go
together when those keys stop naming the same Images: a folder Library is
removed, or RomM is pointed at another instance or library (whose ROM ids name
other games). Both paths call :func:`forget`, so a new record kind is added
here once rather than to two lists that can drift. Blocking.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

# Held while a Library's removal is marked, and while a run captures the
# Library's generation: a run that starts once removal has begun is refused.
_removal_lock = threading.Lock()
_removing: set[str] = set()


def begin_removal(library_id: str) -> None:
    """Mark *library_id* as being removed, before its generation moves."""
    with _removal_lock:
        _removing.add(library_id)


def end_removal(library_id: str) -> None:
    """Its removal is over (its records are gone, it is no longer listed)."""
    with _removal_lock:
        _removing.discard(library_id)


def capture_unless_removing(library_id: str, capture: Callable[[], int]) -> int | None:
    """*capture()* (the Library's generation), or None when its removal has
    begun, under the lock the removal marks it with."""
    with _removal_lock:
        if library_id in _removing:
            return None
        return capture()


def forget(library_id: str) -> None:
    """Drop every record kept for *library_id*'s Images. Blocking.

    The caller has already moved the Library's generation (removed it, or
    bumped it for RomM), so a run in flight writes nothing afterwards.
    """
    from services import duplicates, findings, image_checks

    findings.forget_library(library_id)
    duplicates.forget_library(library_id)
    image_checks.forget_library(library_id)
