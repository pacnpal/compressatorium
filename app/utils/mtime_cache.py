"""Bounded, mtime-aware in-process cache for expensive per-file reads.

Shared infrastructure for archive readers: ``ArchiveService.list_archive_contents``
and ``RomzService.is_single_rom_archive`` both open and parse an archive to answer
a question about its members. A directory listing — or the ``/archive-summary``
batch that hydrates archive badges, or a re-navigation to the same folder — would
otherwise re-open and re-parse every archive each time. This memoizes the result
keyed by ``(path, mtime_ns, ctime_ns, size)``: any write that replaces the file
bumps mtime/ctime and/or size and invalidates the entry, so a conversion that
rewrites an archive can't be served a stale listing (ctime is included because it
can't be reset via ``utime()`` and so survives a same-size rewrite that lands on
the same coarse mtime tick). The cache is LRU-bounded so memory stays flat over
very large libraries.

Values are treated as immutable by the cache — callers that hand back mutable
structures (e.g. lists of dicts) must copy before mutating, since every hit
returns the same shared object.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

# Generous default: one entry per archive. At a few hundred bytes of Python
# objects per cached listing this stays well under a few MB even when full.
DEFAULT_MAX_ENTRIES = 8192


class MtimeCache(Generic[T]):
    """Thread-safe LRU cache keyed by a path's ``(mtime_ns, ctime_ns, size)`` stat."""

    def __init__(self, maxsize: int = DEFAULT_MAX_ENTRIES) -> None:
        self._max = max(1, int(maxsize))
        self._lock = threading.Lock()
        # path -> (stat_key, value), ordered oldest-first for LRU eviction.
        self._store: OrderedDict[str, tuple[tuple[int, int, int], T]] = OrderedDict()

    @staticmethod
    def _stat_key(path: str) -> tuple[int, int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        # Include ctime alongside mtime+size: an in-place rewrite that preserves
        # byte size and lands on the same coarse mtime tick (rapid rewrites on
        # overlay/NAS filesystems) still bumps ctime — which, unlike mtime, can't
        # be set back via utime() — so the cache won't serve a stale listing.
        return (st.st_mtime_ns, st.st_ctime_ns, st.st_size)

    def get_or_compute(
        self,
        path: str,
        compute: Callable[[], T],
        should_cache: Callable[[T], bool] | None = None,
    ) -> T:
        """Return the cached value for ``path`` or compute, store, and return it.

        If ``path`` can't be ``stat``'d (missing/permission), the result is
        computed but not cached — there's no stable key to invalidate against,
        and the next read will recompute anyway. ``should_cache``, when given, is
        called with the freshly computed value and must return ``True`` for it to
        be stored; this lets callers keep memory bounded by declining to retain
        unusually large values (they're recomputed on the next read instead).
        """
        key = self._stat_key(path)
        if key is not None:
            with self._lock:
                cached = self._store.get(path)
                if cached is not None and cached[0] == key:
                    self._store.move_to_end(path)
                    return cached[1]

        value = compute()

        if key is not None and (should_cache is None or should_cache(value)):
            with self._lock:
                self._store[path] = (key, value)
                self._store.move_to_end(path)
                while len(self._store) > self._max:
                    self._store.popitem(last=False)
        return value

    def invalidate(self, path: str) -> None:
        """Drop any cached value for ``path`` (e.g. after a known write)."""
        with self._lock:
            self._store.pop(path, None)

    def clear(self) -> None:
        """Drop every cached entry (used by tests for isolation)."""
        with self._lock:
            self._store.clear()
