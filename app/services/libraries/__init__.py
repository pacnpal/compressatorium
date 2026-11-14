"""The Libraries the app knows: RomM, and every folder registered as one (#303).

RomM is always present (:data:`services.romm.library.romm_library`). Folder
Libraries are rows of the ``folder_libraries`` table, loaded once and kept in
memory; registering, editing or removing one replaces the cached copy. The
scheduler asks :func:`all_libraries` on every tick, so a change takes effect
without a restart.

Registering checks the folder with the same path-safety rules the file browser
uses (inside a configured Volume, resolved, a real folder) and refuses a folder
that is, contains or sits inside one already registered: the same Image under
two Libraries would have two Rules. Removing a Library drops its Rules and run
state and never touches a file.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import db as _db
from services.libraries.folder import ID_PREFIX, FolderLibrary
from services.rules.library import Library, duplicate_actions
from utils.path_utils import ensure_path_within_volumes

__all__ = [
    "FolderLibrary",
    "LibraryPathError",
    "all_libraries",
    "describe",
    "folder_libraries",
    "get",
    "register",
    "remove",
    "update",
]

# Folder Libraries by row id. None until first read.
_cache: dict[int, FolderLibrary] | None = None
_lock = threading.Lock()

MAX_PER_SWEEP_LIMIT = 1000


class LibraryPathError(ValueError):
    """A folder cannot be registered. ``status`` is the HTTP status that fits."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("libraries: db.init_engine() has not run")
    return _db.SessionLocal()


def _from_row(row: _db.FolderLibraryRecord) -> FolderLibrary:
    return FolderLibrary(
        record_id=row.id, root=row.root, name=row.name,
        schedule_enabled=bool(row.schedule_enabled),
        max_per_sweep=int(row.max_per_sweep),
    )


def _loaded() -> dict[int, FolderLibrary]:
    """The cached folder Libraries, read from the database on first use."""
    global _cache
    with _lock:
        if _cache is None:
            with _session() as session:
                rows = session.query(_db.FolderLibraryRecord).order_by(
                    _db.FolderLibraryRecord.id,
                ).all()
                _cache = {row.id: _from_row(row) for row in rows}
        return _cache


def folder_libraries() -> list[FolderLibrary]:
    return list(_loaded().values())


def all_libraries() -> list[Library]:
    """RomM first, then the folder Libraries in the order they were added."""
    from services.romm.library import romm_library

    return [romm_library, *folder_libraries()]


def get(library_id: str) -> Library | None:
    for library in all_libraries():
        if library.id == library_id:
            return library
    return None


def _record_id(library_id: str) -> int | None:
    if not library_id.startswith(ID_PREFIX):
        return None
    try:
        return int(library_id[len(ID_PREFIX):])
    except ValueError:
        return None


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def register(path: str, name: str | None = None) -> FolderLibrary:
    """Register the folder at *path*. Blocking (resolves and stats)."""
    try:
        root = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise LibraryPathError(403, "Access denied: path outside configured volumes") from None
    if not root.is_dir():
        raise LibraryPathError(404, "Folder not found")
    for existing in folder_libraries():
        if _overlaps(root, Path(existing.root)):
            raise LibraryPathError(
                409, f"Overlaps the Library {existing.name!r} at {existing.root}",
            )
    label = (name or "").strip() or root.name or str(root)
    with _lock, _session() as session:
        row = _db.FolderLibraryRecord(
            root=str(root), name=label, schedule_enabled=False, max_per_sweep=25,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        session.add(row)
        session.commit()
        library = _from_row(row)
        if _cache is not None:
            _cache[library.record_id] = library
    return library


def update(
    library_id: str, *, name: str | None = None,
    schedule_enabled: bool | None = None, max_per_sweep: int | None = None,
) -> FolderLibrary | None:
    """Change a folder Library's name or settings. None when there is no such Library."""
    record_id = _record_id(library_id)
    if record_id is None:
        return None
    _loaded()
    with _lock, _session() as session:
        row = session.get(_db.FolderLibraryRecord, record_id)
        if row is None:
            return None
        if name is not None and name.strip():
            row.name = name.strip()
        if schedule_enabled is not None:
            row.schedule_enabled = bool(schedule_enabled)
        if max_per_sweep is not None:
            row.max_per_sweep = max(1, min(int(max_per_sweep), MAX_PER_SWEEP_LIMIT))
        session.commit()
        library = _from_row(row)
        if _cache is not None:
            _cache[record_id] = library
    return library


async def remove(library_id: str) -> bool:
    """Forget a folder Library, its Rules and its run state. Touches no files."""
    from fastapi.concurrency import run_in_threadpool
    from services.rules import sweep as rules_sweep

    record_id = _record_id(library_id)
    library = _loaded().get(record_id) if record_id is not None else None
    if library is None:
        return False
    await rules_sweep.forget_library(library)

    def _delete() -> None:
        with _lock, _session() as session:
            row = session.get(_db.FolderLibraryRecord, record_id)
            if row is not None:
                session.delete(row)
                session.commit()
            if _cache is not None:
                _cache.pop(record_id, None)

    await run_in_threadpool(_delete)
    return True


def describe(library: Library) -> dict[str, Any]:
    """A Library as the API lists it."""
    return {
        "id": library.id,
        "kind": library.kind,
        "name": library.name,
        "root": library.root,
        "configured": library.configured(),
        "capabilities": sorted(library.capabilities),
        "duplicate_actions": list(duplicate_actions(library)),
        "schedule_enabled": library.schedule_enabled(),
        "max_per_sweep": library.max_per_sweep(),
    }
