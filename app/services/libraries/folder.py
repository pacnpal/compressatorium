"""A folder as a Library: the second implementation of :class:`services.rules.Library`.

One root folder inside a Volume. The files under it are grouped into Images
(:func:`services.image_sets.group_images`): a ``.cue`` or ``.gdi`` owns the
tracks it references, a split dump's first part owns the rest, and playlists
are never Images. Each Image's Platform comes from Platform detection
(:func:`services.platform_detect.detect_uncached`), so an Image in a ``psx``
folder, or with a PS1 header, belongs to the ``psx`` Platform.

* A Platform's id is its slug (``psx``, ``ngc``); its name is the display name.
* An Image's key is its path relative to the root, with ``/`` separators.
* Conflicted and Unsorted Images belong to no Platform, so no Rule sees them
  and no sweep queues them. Nor does a file that cannot be read.
* ``recognized`` is the cached DAT or Hasheous match for the file, read in one
  query per listing. Unknown (None) when nothing usable is cached or the cache
  cannot be read; the engine's Recognized filters pass neither way on None.
  Nothing is hashed here.

**One walk.** A walk of the root is kept for :data:`SCAN_TTL_SECONDS`, so a
sweep over several ruled Platforms, and the listing that preceded it, share
it. Each file's detection and sheet members are cached on the Library, keyed
by path and checked against size and mtime, and pruned to the files the last
walk saw. A repeat walk of a large Library is a stat per file and reads no
headers.

**No disk on the event loop.** :meth:`FolderLibrary.configured` answers from
memory (the root was checked against the Volumes when it was registered). A
root that has gone, or a mount that stops answering, shows up in the walk,
which the engine runs detached and bounded.

Folder Rules support the skip policy only (no ``CONVERTED_HISTORY``), and a
failed Image waits until its file changes (``FAILED_HOLD``).
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from logging_setup import get_logger
from services import image_sets, platform_detect
from services.platform_detect import CONFLICT, Detection
from services.rules.library import (
    FAILED_HOLD,
    RECOGNIZED_FILTER,
    Image,
    LibraryUnreadable,
    Platform,
    QueuedImage,
)
from services.tools import registry
from utils.path_utils import match_extension

logger = get_logger("folder_library")

ID_PREFIX = "folder:"

# How long one walk serves later listings. Long enough to cover a sweep over
# every ruled Platform; short enough that the Rules view is current.
SCAN_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class Entry:
    """One Image found under the root (its primary file)."""

    key: str
    path: str
    size: int
    mtime: float
    detection: Detection


@dataclass
class Scan:
    """What one walk of the root found."""

    entries: list[Entry] = field(default_factory=list)
    # Image files and folders that could not be read. Never queued.
    unreadable: int = 0
    taken_at: float = 0.0

    def by_platform(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = {}
        for entry in self.entries:
            if entry.detection.platform:
                out.setdefault(entry.detection.platform, []).append(entry)
        return out


def _image_extensions() -> tuple[str, ...]:
    """Every extension a registered tool reads or writes."""
    return tuple(sorted(
        set(registry.convertible_extensions()) | set(registry.output_extensions())
    ))


def _recognized(paths: list[str]) -> dict[str, bool | None]:
    """The cached DAT or Hasheous verdict for each path, in one query.

    True for a cached match from either source, False for a cached miss that
    is still valid for the lookup sources now configured, None when there is
    no usable row or the cache cannot be read. Never hashes.
    """
    if not paths:
        return {}
    # The usability rule lives with the match routes that write these rows;
    # imported lazily, as dat_sync does, to keep the import graph one-way.
    from routes.dat import cached_result_usable
    from services.dat_store import dat_store

    try:
        cached = dat_store.get_matches_batch(paths)
    except Exception:  # the match cache is a hint here, never a failure
        # Unknown, not "not Recognized": the engine's filters pass neither way
        # on None, so an only_unmatched Rule queues nothing rather than all.
        logger.warning("folder_library: could not read the match cache", exc_info=True)
        return dict.fromkeys(paths)
    out: dict[str, bool | None] = {}
    for path in paths:
        row = cached.get(path)
        out[path] = bool(row.get("matched")) if cached_result_usable(row) else None
    return out


class FolderLibrary:
    """A folder registered as a Library."""

    kind = "folder"
    capabilities = frozenset({RECOGNIZED_FILTER, FAILED_HOLD})
    legacy_preference_keys = MappingProxyType({})

    def __init__(
        self, *, record_id: int, root: str, name: str,
        schedule_enabled: bool = False, max_per_sweep: int = 25,
    ) -> None:
        self.record_id = record_id
        self.id = f"{ID_PREFIX}{record_id}"
        self.root = root
        self.name = name
        self._schedule_enabled = schedule_enabled
        self._max_per_sweep = max_per_sweep
        self._lock = threading.Lock()
        self._scan: Scan | None = None
        # path -> (size, mtime_ns, value), for the files the last walk saw.
        self._detections: dict[str, tuple[int, int, Detection]] = {}
        self._members: dict[str, tuple[int, int, list[str]]] = {}

    def __repr__(self) -> str:
        return f"FolderLibrary(id={self.id!r}, root={self.root!r})"

    def adopt_cache(self, other: FolderLibrary) -> None:
        """Keep *other*'s walk and caches (the same Library, re-read after an edit)."""
        with other._lock:
            self._scan = other._scan
            self._detections = other._detections
            self._members = other._members

    # ---- the walk ----

    def _files(self) -> tuple[list[tuple[str, os.stat_result]], int]:
        """Every regular, readable file with an Image extension, and how many were not readable."""
        root = self.root
        if not os.path.isdir(root):
            raise LibraryUnreadable(f"{root} is not a folder")
        extensions = _image_extensions()
        files: list[tuple[str, os.stat_result]] = []
        unreadable = 0
        root_errors: list[OSError] = []

        def on_error(exc: OSError) -> None:
            nonlocal unreadable
            if os.path.normpath(getattr(exc, "filename", "") or "") == os.path.normpath(root):
                root_errors.append(exc)
            else:
                unreadable += 1

        for current, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
            dirs.sort()
            for name in sorted(names):
                if match_extension(name, extensions) is None:
                    continue
                path = os.path.join(current, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    unreadable += 1
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if not os.access(path, os.R_OK):
                    unreadable += 1
                    continue
                files.append((path, st))
        if root_errors:
            raise LibraryUnreadable(str(root_errors[0]))
        return files, unreadable

    def _walk(self) -> Scan:
        """Walk the root once, group the files into Images and detect each Platform."""
        files, unreadable = self._files()
        stats = dict(files)
        members_cache: dict[str, tuple[int, int, list[str]]] = {}
        detections: dict[str, tuple[int, int, Detection]] = {}

        def members(path: str) -> list[str]:
            st = stats[path]
            hit = self._members.get(path)
            if hit is not None and hit[:2] == (st.st_size, st.st_mtime_ns):
                value = hit[2]
            else:
                value = image_sets.image_members(path)
            members_cache[path] = (st.st_size, st.st_mtime_ns, value)
            return value

        scan = Scan(unreadable=unreadable, taken_at=time.monotonic())
        root = self.root
        for path in image_sets.group_images([p for p, _ in files], members=members):
            st = stats[path]
            hit = self._detections.get(path)
            if hit is not None and hit[:2] == (st.st_size, st.st_mtime_ns):
                detection = hit[2]
            else:
                detection = platform_detect.detect_uncached(path)
            detections[path] = (st.st_size, st.st_mtime_ns, detection)
            scan.entries.append(Entry(
                key=Path(os.path.relpath(path, root)).as_posix(),
                path=path, size=st.st_size, mtime=st.st_mtime, detection=detection,
            ))
        # Only what this walk saw, so the caches are as large as the Library.
        self._members = members_cache
        self._detections = detections
        return scan

    def scan(self, *, refresh: bool = False) -> Scan:
        """The latest walk, taking a new one when asked or when it is stale. Blocking."""
        with self._lock:
            fresh = (
                self._scan is not None
                and time.monotonic() - self._scan.taken_at < SCAN_TTL_SECONDS
            )
            if refresh or not fresh:
                self._scan = self._walk()
            return self._scan

    def summary(self, *, refresh: bool = False) -> dict[str, Any]:
        """Detected Platforms with Image counts, and the Images left out."""
        scan = self.scan(refresh=refresh)
        unsorted = conflicted = 0
        for entry in scan.entries:
            if entry.detection.platform:
                continue
            if entry.detection.how == CONFLICT:
                conflicted += 1
            else:
                unsorted += 1
        return {
            "platforms": [
                {
                    "id": slug, "slug": slug,
                    "name": platform_detect.platform_name(slug), "images": len(entries),
                }
                for slug, entries in sorted(scan.by_platform().items())
            ],
            "unsorted": unsorted,
            "conflicted": conflicted,
            "unreadable": scan.unreadable,
        }

    # ---- the Library interface ----

    def configured(self) -> bool:
        """Always: the root was checked when it was registered. No disk access.

        The engine calls this on the event loop. A root that has gone since is
        found by the walk, off the loop, and reported as unreadable.
        """
        return True

    def canonical_platform_id(self, raw: Any) -> str | None:
        if not isinstance(raw, str) or ":" in raw:
            return None
        return platform_detect.known_slug(raw)

    def platform_value(self, platform_id: str) -> str:
        return platform_id

    def platforms(self) -> list[Platform]:
        return [
            Platform(id=p["id"], slug=p["slug"], name=p["name"])
            for p in self.summary()["platforms"]
        ]

    def images(self, platform_id: str) -> list[Image]:
        try:
            scan = self.scan()
        except OSError as exc:
            raise LibraryUnreadable(str(exc)) from exc
        entries = scan.by_platform().get(platform_id, [])
        recognized = _recognized([e.path for e in entries])
        return [
            Image(
                key=e.key,
                name=os.path.basename(e.path),
                title=e.key,
                size=e.size,
                added_at=e.mtime,
                recognized=recognized.get(e.path),
                platform=platform_id,
                path=e.path,
                record=e,
            )
            for e in entries
        ]

    def rule_defaults(self) -> dict[str, Any]:
        return {}

    def max_per_sweep(self) -> int:
        return int(self._max_per_sweep)

    def schedule_enabled(self) -> bool:
        return bool(self._schedule_enabled)

    # ---- hooks: a folder Library has no steps of its own ----

    def sweep_blocked(self) -> str | None:
        return None

    def summary_fields(self) -> dict[str, Any]:
        return {}

    async def after_queue(
        self, rule: dict, spec: Any, queued: list[QueuedImage],
        summary: dict, result: dict,
    ) -> None:
        return None

    async def sweep_extras(self) -> dict[str, Any]:
        return {}
