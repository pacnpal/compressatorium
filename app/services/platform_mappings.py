"""The user's folder-to-Platform mappings: "this folder is PSP" (#304).

A mapping belongs to a Library and names a folder by its path relative to the
Library root, so it is stored as ``(library_id, folder)``. Platform detection
walks up from an Image's folder: the first folder with a mapping or a Platform
name decides, and at the same folder the mapping wins (:func:`mapped_folders`).
The header still beats both. A mapping that disagrees with a header is a
conflict, never a guess.

The mappings are kept in memory as an index of absolute folder paths, built
from the database on first use and dropped on every change. Detection puts the
deciding folder (path, source, slug) into its cache key
(``platform_detect.detection_key``), so a
changed mapping re-detects exactly the Images under that folder, in the shared
cache and in a folder Library's own.

Setting a mapping never moves, renames or deletes a file.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from services import db as _db
from utils.path_utils import ensure_path_within_volumes

logger = get_logger("platform_mappings")

# Absolute folder path -> Platform slug. None until first read.
_index: dict[str, str] | None = None
# Bumped on every change, so an index built from rows read before the change
# is never kept.
_generation = 0
_lock = threading.Lock()


class MappingsUnavailable(RuntimeError):
    """The mappings could not be read: no answer, which is not "none set".

    A read that fails must not pass for an empty table, or an Image whose
    folder is mapped would be decided without that evidence (a header taken
    over the mapping it disagrees with). Detection answers Unsorted and a
    folder Library's Rules listing stops until the mappings read again.
    """


class MappingError(ValueError):
    """A mapping cannot be set. ``status`` is the HTTP status that fits."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def invalidate() -> None:
    """Forget the in-memory index; the next lookup reads the database again."""
    global _index, _generation
    with _lock:
        _index = None
        _generation += 1


def reset_for_tests() -> None:
    invalidate()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _rel(folder: Path, root: str) -> str:
    rel = Path(os.path.relpath(folder, root)).as_posix()
    return "" if rel == "." else rel


def _build_index() -> dict[str, str]:
    from services import libraries

    with _db.SessionLocal() as session:
        rows = [
            (r.library_id, r.folder, r.platform)
            for r in session.query(_db.PlatformMappingRecord).all()
        ]
    index: dict[str, str] = {}
    placed = []
    for library_id, folder, platform in rows:
        library = libraries.get(library_id)
        if library is None or not library.root:
            continue
        placed.append((library.root, folder, platform))
    # Libraries may nest (#514): where two map one folder, the nearer root's
    # mapping (the folder's owner) is written last, so it wins.
    for root, folder, platform in sorted(placed, key=lambda row: len(Path(row[0]).parts)):
        index[os.path.normpath(os.path.join(root, folder))] = platform
    return index


def _loaded() -> dict[str, str]:
    global _index
    with _lock:
        if _index is not None:
            return _index
        generation = _generation
    if _db.SessionLocal is None:
        # No database in this process (a CLI dry run on a fresh data dir):
        # there is no mapping table, so there is none set.
        return {}
    try:
        built = _build_index()
    except Exception as exc:
        # Never read a failed read as "none set": nothing is cached, and the
        # caller decides nothing on missing evidence.
        logger.warning("platform_mappings: could not read the mappings", exc_info=True)
        raise MappingsUnavailable("the folder Platform mappings could not be read") from exc
    with _lock:
        if _generation == generation:
            _index = built
    return built


def mapped_folders() -> dict[str, str]:
    """Every mapped folder: absolute path -> Platform slug. Do not modify it.

    Platform detection walks an Image's parents against it, together with the
    Platform folder names (``platform_detect._place``). Blocking on first use
    (reads the database); every later call returns the cached index. Raises
    :class:`MappingsUnavailable` when the database read fails.
    """
    return _loaded()


def _folder_library_for(folder: Path):
    """The folder Library with the nearest root holding *folder* (#514)."""
    from services import libraries

    holding = [
        library for library in libraries.folder_libraries()
        if folder.is_relative_to(Path(library.root))
    ]
    return max(holding, key=lambda library: len(Path(library.root).parts), default=None)


def set_mapping(path: str, platform: str | None) -> dict[str, Any]:
    """Set (or, with no *platform*, clear) the Platform of the folder at *path*.

    The folder must be inside a folder Library. Blocking (resolves and stats);
    never on the event loop.
    """
    from services.platform_detect import known_slug, platform_name

    try:
        folder = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise MappingError(403, "Access denied: path outside configured volumes") from None
    if not folder.is_dir():
        raise MappingError(404, "Folder not found")
    library = _folder_library_for(folder)
    if library is None:
        raise MappingError(404, "This folder is not inside a Library")
    slug = None
    if platform:
        slug = known_slug(platform)
        if slug is None:
            raise MappingError(422, f"Unknown Platform: {platform}")
    rel = _rel(folder, library.root)
    with _db.SessionLocal() as session:
        row = session.query(_db.PlatformMappingRecord).filter_by(
            library_id=library.id, folder=rel,
        ).one_or_none()
        if slug is None:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(_db.PlatformMappingRecord(
                library_id=library.id, folder=rel, platform=slug, updated_at=_now(),
            ))
        else:
            row.platform = slug
            row.updated_at = _now()
        session.commit()
    invalidate()
    # The Library's last walk was taken under the old mapping.
    library.invalidate()
    return {
        "library_id": library.id,
        "folder": rel,
        "path": str(folder),
        "platform": slug,
        "platform_name": platform_name(slug),
    }


def mappings() -> list[dict[str, Any]]:
    """Every mapping, as the API lists it. Blocking."""
    from services import libraries
    from services.platform_detect import platform_name

    with _db.SessionLocal() as session:
        rows = session.query(_db.PlatformMappingRecord).order_by(
            _db.PlatformMappingRecord.library_id, _db.PlatformMappingRecord.folder,
        ).all()
    out = []
    for row in rows:
        library = libraries.get(row.library_id)
        out.append({
            "library_id": row.library_id,
            "folder": row.folder,
            "path": (
                os.path.normpath(os.path.join(library.root, row.folder))
                if library is not None and library.root else None
            ),
            "platform": row.platform,
            "platform_name": platform_name(row.platform),
        })
    return out


def forget_library(library_id: str) -> None:
    """Drop every mapping of a removed Library. Blocking."""
    with _db.SessionLocal() as session:
        session.query(_db.PlatformMappingRecord).filter_by(library_id=library_id).delete()
        session.commit()
    invalidate()
