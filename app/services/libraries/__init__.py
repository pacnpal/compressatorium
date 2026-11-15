"""The Libraries the app knows: RomM, and every folder registered as one (#303).

RomM is always present (:data:`services.romm.library.romm_library`). Folder
Libraries are rows of the ``folder_libraries`` table, loaded once and kept in
memory; registering, editing or removing one replaces the cached copy. The
scheduler asks :func:`all_libraries` on every tick, so a change takes effect
without a restart.

Registering checks the folder with the same path-safety rules the file browser
uses (inside a configured Volume, resolved, a real folder) and refuses a folder
that is, contains or sits inside another Library's root (a registered folder,
or RomM's library folder): the same Image under two Libraries would have two
Rules. The check and the insert happen under one lock, and the database's
unique root is the backstop. Removing a Library drops its Rules and run state
and never touches a file.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import db as _db
from services.libraries.folder import ID_PREFIX, FolderLibrary
from services.rules.library import Library, duplicate_actions
from sqlalchemy.exc import IntegrityError
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
    "run_forever",
    "update",
]

# Folder Libraries by row id. None until first read.
_cache: dict[int, FolderLibrary] | None = None
# Guards `_cache` and nothing else. The event loop takes it (every `get()`),
# so it is never held across disk or database access, nor across a lock that
# is (a Library's walk lock).
_lock = threading.Lock()
# Serialises the overlap check and insert of `register()`. Held across a
# resolve and a database write, so only ever taken in a worker thread.
_register_lock = threading.Lock()

MAX_PER_SWEEP_LIMIT = 1000
# How long a registration waits for another one to finish before answering
# "still running". Short: the other may be stuck on a dead RomM mount.
REGISTER_WAIT_SECONDS = 5.0


class LibraryPathError(ValueError):
    """A folder cannot be registered. ``status`` is the HTTP status that fits."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def reset_for_tests() -> None:
    global _cache
    from services import platform_mappings

    with _lock:
        _cache = None
    platform_mappings.reset_for_tests()


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
        if _cache is not None:
            return _cache
    # Read outside the lock; two first reads race harmlessly, the first wins.
    with _session() as session:
        rows = session.query(_db.FolderLibraryRecord).order_by(
            _db.FolderLibraryRecord.id,
        ).all()
        loaded = {row.id: _from_row(row) for row in rows}
    with _lock:
        if _cache is None:
            _cache = loaded
        return _cache


def folder_libraries() -> list[FolderLibrary]:
    return list(_loaded().values())


def all_libraries() -> list[Library]:
    """RomM first, then the folder Libraries in the order they were added."""
    from services.romm.library import romm_library

    return [romm_library, *folder_libraries()]


async def run_forever() -> None:
    """The scheduler: every Library whose switch is on, on its own Rules' clocks."""
    from services.rules import sweep as rules_sweep

    await rules_sweep.run_forever(all_libraries)


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


def _romm_root() -> Path | None:
    """RomM's library folder, resolved. Touches the disk (RomM's mount)."""
    from services.romm.library import romm_library

    if not romm_library.root:
        return None
    try:
        return Path(romm_library.root).resolve()
    except (OSError, RuntimeError):
        return Path(romm_library.root)


def register(path: str, name: str | None = None) -> FolderLibrary:
    """Register the folder at *path*. Blocking (resolves and stats); never on the event loop."""
    from services.romm.library import romm_library

    try:
        root = ensure_path_within_volumes(path, treat_archives=False)
    except ValueError:
        raise LibraryPathError(403, "Access denied: path outside configured volumes") from None
    if not root.is_dir():
        raise LibraryPathError(404, "Folder not found")
    label = (name or "").strip() or root.name or str(root)
    _loaded()
    # Check and insert as one step, so two registrations of nested folders
    # cannot both pass the check. `_register_lock`, not `_lock`: this resolves
    # RomM's root and writes the database, and `_lock` is taken by the loop.
    # Bounded: a registration stuck on a dead RomM mount must not queue every
    # later one behind it.
    if not _register_lock.acquire(timeout=REGISTER_WAIT_SECONDS):
        raise LibraryPathError(409, "Another registration is still running; try again shortly")
    try:
        others = [(lib.name, Path(lib.root)) for lib in folder_libraries()]
        romm_root = _romm_root()
        if romm_root is not None:
            others.append((romm_library.name, romm_root))
        for other_name, other_root in others:
            if root == other_root:
                # Most often a retry after a 504: the first registration went
                # on and finished.
                raise LibraryPathError(
                    409,
                    f"Already the Library {other_name!r}: an earlier registration "
                    "of this folder has finished",
                )
            if _overlaps(root, other_root):
                raise LibraryPathError(
                    409, f"Overlaps the Library {other_name!r} at {other_root}",
                )
        with _session() as session:
            row = _db.FolderLibraryRecord(
                root=str(root), name=label, schedule_enabled=False, max_per_sweep=25,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Another process registered the same root first.
                session.rollback()
                raise LibraryPathError(409, "Already a Library") from None
            library = _from_row(row)
        with _lock:
            if _cache is not None:
                _cache[library.record_id] = library
    finally:
        _register_lock.release()
    return library


def update(
    library_id: str, *, name: str | None = None,
    schedule_enabled: bool | None = None, max_per_sweep: int | None = None,
) -> FolderLibrary | None:
    """Change a folder Library's name or settings. None when there is no such Library.

    Writes the row, then changes the cached Library in place: a walk of it may
    be running (or stuck on a dead mount), and none of these settings needs it.
    """
    record_id = _record_id(library_id)
    if record_id is None:
        return None
    library = _loaded().get(record_id)
    if library is None:
        return None
    if max_per_sweep is not None:
        max_per_sweep = max(1, min(int(max_per_sweep), MAX_PER_SWEEP_LIMIT))
    label = name.strip() if name is not None and name.strip() else None
    # The row and the object change as one step, so two concurrent edits
    # cannot leave memory saying one thing and the database another.
    with library.settings_lock:
        with _session() as session:
            row = session.get(_db.FolderLibraryRecord, record_id)
            if row is None:
                return None
            if label is not None:
                row.name = label
            if schedule_enabled is not None:
                row.schedule_enabled = bool(schedule_enabled)
            if max_per_sweep is not None:
                row.max_per_sweep = max_per_sweep
            session.commit()
        library.apply_settings(
            name=label, schedule_enabled=schedule_enabled, max_per_sweep=max_per_sweep,
        )
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
        from services import findings, platform_mappings

        # First, so a walk of it that finishes from here on writes nothing.
        library.mark_removed()
        platform_mappings.forget_library(library_id)
        findings.forget_library(library_id)
        with _session() as session:
            row = session.get(_db.FolderLibraryRecord, record_id)
            if row is not None:
                session.delete(row)
                session.commit()
        with _lock:
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
        "capabilities": sorted(library.capabilities),
        "duplicate_actions": list(duplicate_actions(library)),
        "schedule_enabled": library.schedule_enabled(),
        "max_per_sweep": library.max_per_sweep(),
    }
