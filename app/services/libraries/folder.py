"""A folder as a Library: the second implementation of :class:`services.rules.Library`.

One root folder inside a Volume. Every file under it whose extension a
registered tool reads or writes is an Image. Its Platform comes from Platform
detection (:func:`services.platform_detect.detect_platform`), so an Image in a
``ps2`` folder, or with a PS2 header, belongs to the ``ps2`` Platform.

* A Platform's id is its slug (``ps2``, ``ngc``).
* An Image's key is its path relative to the root, with ``/`` separators.
* Conflicted and Unsorted Images belong to no Platform, so no Rule sees them
  and no sweep queues them. Nor does a file that cannot be read.
* ``recognized`` is the cached DAT or Hasheous match for the file, read in one
  query per listing. A file nobody has matched yet is unknown (None). Nothing
  is hashed here.

Folder Rules support the skip policy only. The Library does not declare
``CONVERTED_HISTORY``, and the engine refuses ``overwrite`` and ``rename``
without it (see ``services.rules.library.duplicate_actions``).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from logging_setup import get_logger
from services import platform_detect
from services.platform_detect import CONFLICT, Detection
from services.rules.library import (
    RECOGNIZED_FILTER,
    Image,
    LibraryUnreadable,
    Platform,
    QueuedImage,
)
from services.tools import registry
from utils.path_utils import is_within_configured_volumes, match_extension

logger = get_logger("folder_library")

ID_PREFIX = "folder:"


@dataclass(frozen=True)
class Entry:
    """One Image file found under the root."""

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


def _image_extensions() -> tuple[str, ...]:
    """Every extension a registered tool reads or writes."""
    return tuple(sorted(
        set(registry.convertible_extensions()) | set(registry.output_extensions())
    ))


def _recognized(paths: list[str]) -> dict[str, bool | None]:
    """The cached DAT or Hasheous verdict for each path, in one query.

    True for a cached match from either source, False for a cached miss that
    is still valid for the lookup sources now configured, None when there is
    no usable row. Never hashes.
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
    capabilities = frozenset({RECOGNIZED_FILTER})
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

    def __repr__(self) -> str:
        return f"FolderLibrary(id={self.id!r}, root={self.root!r})"

    # ---- the walk ----

    def scan(self) -> Scan:
        """Walk the root once. Blocking: a stat per file and Platform detection.

        Symlinks are not followed and symlinked files are not Images, so
        nothing outside the root is listed. Raises :class:`LibraryUnreadable`
        when the root itself cannot be listed.
        """
        root = self.root
        if not os.path.isdir(root):
            raise LibraryUnreadable(f"{root} is not a folder")
        extensions = _image_extensions()
        result = Scan()
        root_errors: list[OSError] = []

        def on_error(exc: OSError) -> None:
            if os.path.normpath(getattr(exc, "filename", "") or "") == os.path.normpath(root):
                root_errors.append(exc)
            else:
                result.unreadable += 1

        for current, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
            dirs.sort()
            for name in sorted(files):
                if match_extension(name, extensions) is None:
                    continue
                path = os.path.join(current, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    result.unreadable += 1
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if not os.access(path, os.R_OK):
                    result.unreadable += 1
                    continue
                key = Path(os.path.relpath(path, root)).as_posix()
                result.entries.append(Entry(
                    key=key, path=path, size=st.st_size, mtime=st.st_mtime,
                    detection=platform_detect.detect_platform(path),
                ))
        if root_errors:
            raise LibraryUnreadable(str(root_errors[0]))
        return result

    def summary(self) -> dict[str, Any]:
        """Detected Platforms with Image counts, and the Images left out."""
        scan = self.scan()
        counts: dict[str, int] = {}
        unsorted = conflicted = 0
        for entry in scan.entries:
            platform = entry.detection.platform
            if platform:
                counts[platform] = counts.get(platform, 0) + 1
            elif entry.detection.how == CONFLICT:
                conflicted += 1
            else:
                unsorted += 1
        return {
            "platforms": [
                {"id": slug, "slug": slug, "name": slug, "images": n}
                for slug, n in sorted(counts.items())
            ],
            "unsorted": unsorted,
            "conflicted": conflicted,
            "unreadable": scan.unreadable,
        }

    # ---- the Library interface ----

    def configured(self) -> bool:
        return os.path.isdir(self.root) and is_within_configured_volumes(self.root)

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
        entries = [e for e in scan.entries if e.detection.platform == platform_id]
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
