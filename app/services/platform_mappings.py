"""The user's folder-to-Platform mappings: "this folder is PSP" (#304).

A mapping belongs to a Library and names a folder by its path relative to the
Library root, so it is stored as ``(library_id, folder)``. Platform detection
asks :func:`mapping_for` for the nearest mapped folder above an Image and
treats the answer as evidence: below the header, above the folder name. A
mapping that disagrees with a header is a conflict, never a guess.

The mappings are kept in memory as an index of absolute folder paths, built
from the database on first use and dropped on every change. Detection puts the
mapping it used into its cache key (``platform_detect.detection_key``), so a
changed mapping re-detects exactly the Images under that folder, in the shared
cache and in a folder Library's own.

Setting a mapping never moves, renames or deletes a file.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePath
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
    for library_id, folder, platform in rows:
        library = libraries.get(library_id)
        if library is None or not library.root:
            continue
        index[os.path.normpath(os.path.join(library.root, folder))] = platform
    return index


def _loaded() -> dict[str, str]:
    global _index
    with _lock:
        if _index is not None:
            return _index
        generation = _generation
    if _db.SessionLocal is None:
        return {}
    try:
        built = _build_index()
    except Exception:
        # Detection still answers from the header and the folder name; say so
        # loudly rather than read a missing mapping as "none set".
        logger.warning("platform_mappings: could not read the mappings", exc_info=True)
        return {}
    with _lock:
        if _generation == generation:
            _index = built
    return built


def mapping_for(path: str) -> tuple[str, str] | None:
    """``(folder name, Platform slug)`` of the nearest mapped folder above *path*.

    None when no folder above it is mapped. Blocking on first use (reads the
    database); every later call is a few dictionary lookups.
    """
    index = _loaded()
    if not index:
        return None
    for parent in PurePath(os.path.normpath(path)).parents:
        slug = index.get(str(parent))
        if slug:
            return parent.name or str(parent), slug
    return None


def _folder_library_for(folder: Path):
    from services import libraries

    for library in libraries.folder_libraries():
        root = Path(library.root)
        if folder == root or folder.is_relative_to(root):
            return library
    return None


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
