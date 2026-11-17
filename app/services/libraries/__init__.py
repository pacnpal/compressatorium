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

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from services import db as _db
from services.job_manager import VOLUME_PROBE_TIMEOUT_SECONDS
from services.libraries.folder import ID_PREFIX, FolderLibrary
from services.rules.library import (
    ES_FRONTEND_REFERRER,
    GAMELISTS_FOLDER,
    Library,
    duplicate_actions,
)
from services.subprocess_runner import ProbeCapacityExceeded, run_detached
from sqlalchemy.exc import IntegrityError
from utils.path_utils import ensure_path_within_volumes, volume_containment

logger = get_logger("libraries")

__all__ = [
    "AmbiguousLibrary",
    "FolderLibrary",
    "LibraryPathError",
    "check_gamelists_folder",
    "all_libraries",
    "describe",
    "folder_libraries",
    "get",
    "library_for",
    "placements",
    "register",
    "remove",
    "run_forever",
    "update",
    "without_a_database",
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
# How long saving RomM's settings waits for the check of its library root
# against the folder Libraries before answering 503: the same bound #294 gives
# the convert routes' Volume checks.
ROOT_CHECK_SECONDS = VOLUME_PROBE_TIMEOUT_SECONDS
# Library folder workers outstanding at once (`run_admitted`): registration,
# the RomM root check, and setting a folder's Platform mapping (#304). A slot
# is held until the worker returns, so a dead mount costs at most this many
# threads however often a request is retried. Room for a few honest requests
# at once; they finish in milliseconds on a healthy mount.
MAX_PENDING_WORKERS = 4
_worker_slots = threading.BoundedSemaphore(MAX_PENDING_WORKERS)


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
        _canonical_roots.clear()
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
        es_frontend=bool(row.es_frontend),
        gamelists_folder=row.gamelists_folder,
        layout=row.layout,
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


def note_job_finished(job) -> None:
    """Tell every Library which paths a finished job changed (#353).

    A conversion that adds or replaces an Image (its output, a Source it
    deleted) leaves a Library's kept walk out of date: a check or conversion
    sweep within ``SCAN_TTL_SECONDS`` would list the Images as they were. Each
    Library drops what it listed from before (``Library.note_paths_changed``).
    On the event loop, so it reads only the Libraries already in memory: a
    catalogue never loaded holds no walk to drop. Never raises.

    An externally-managed job (a check Rule's run, a metadata scan, a DAT
    match) writes no Library file, so it drops no walk: a check job's own end
    must not leave its Rule's run looking stale (#360).
    """
    from services.job_manager import is_external_mode
    from services.romm.library import romm_library

    if is_external_mode(getattr(job, "mode", None)):
        return
    paths = [getattr(job, "output_path", None), getattr(job, "file_path", None)]
    paths = [p for p in paths if p]
    if not paths:
        return
    with _lock:
        folders = list(_cache.values()) if _cache is not None else []
    for library in (romm_library, *folders):
        try:
            library.note_paths_changed(paths)
        except Exception:
            # A listener must never fail a conversion; the walk then ages out.
            logger.warning(
                "libraries: %s could not drop its walk after job %s",
                library.id, getattr(job, "id", None), exc_info=True,
            )


def attach(job_manager) -> None:
    """Register :func:`note_job_finished` on *job_manager*. Idempotent.

    The server's lifespan and the CLI's job runner both call this, so the two
    cannot drift.
    """
    if note_job_finished not in job_manager._terminal_listeners:
        job_manager.add_terminal_listener(note_job_finished)


def without_a_database() -> list[Library]:
    """The Libraries that exist with no database open: RomM alone.

    Folder Libraries are rows of the database, so a process that has none (a
    ``run-rules --dry-run`` on a fresh data dir) lists these instead of
    :func:`all_libraries`, which would fail opening a session.
    """
    from services.romm.library import romm_library

    return [romm_library]


async def run_forever() -> None:
    """The scheduler: every Library whose switch is on, on its own Rules' clocks."""
    from services.rules import sweep as rules_sweep

    await rules_sweep.run_forever(all_libraries)


def get(library_id: str) -> Library | None:
    for library in all_libraries():
        if library.id == library_id:
            return library
    return None


class AmbiguousLibrary(Exception):
    """A file is listed in one Library but lives in another (#413).

    Raised by :func:`library_for`. Which Library's opt-in Referrers cover it
    can't be told, so every caller fails closed: the Referrer hook keeps the
    Source, the staging check hides the output.
    """

    def __init__(self, path: str, listed: Library, lives: Library) -> None:
        super().__init__(
            f"{path} is listed in the Library {listed.name!r} but lives in the "
            f"Library {lives.name!r}, so more than one Library could hold it"
        )
        self.listed = listed
        self.lives = lives


def library_for(path: str) -> Library | None:
    """The Library that holds *path*, or None when no Library does (#383, #413).

    Found from the path as listed first: a folder Library's system folder may
    be a symlink pointing outside its root (``ROMs/psx -> ../elsewhere/psx``),
    and a file reached through the Library's folders is covered by the
    Library's Referrers wherever it lives. The listed path is only normalised
    (made absolute, ``.`` and ``..`` folded), never resolved, and matched
    against each root as stored and as resolved.

    Then where it lives: *path* resolved against each root resolved, once
    (:func:`_canonical_root`, cached), so a root reached through a symlink (a
    symlinked ``ROMM_LIBRARY_ROOT``) still holds its Images (codex
    4588646131), and a file reached from outside every Library through a link
    into one still belongs to it.

    The two agree, or one of them finds nothing: that Library. When they name
    two different Libraries it raises :class:`AmbiguousLibrary` rather than
    pick one. Roots never overlap, so each lookup matches at most one.

    Which Library holds a file decides only which Referrers run. It never
    widens what may be written: containment stays on resolved paths (the job
    refuses a Source outside every Volume before this is asked, and each
    Referrer checks its own targets). With no database open only RomM is
    asked (:func:`without_a_database`).

    Blocking (resolves *path*, a root the first time, reads the folder
    Libraries on first use); callers run it off the event loop, bounded. A
    failed read raises: the Referrer hook then can't tell which opt-in
    Referrers apply, and the caller keeps the Source.
    """
    listed = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    target = _resolved(path)
    candidates = without_a_database() if _db.SessionLocal is None else all_libraries()
    by_listing = by_location = None
    for library in candidates:
        root = library.root
        if not root:
            continue
        canonical = _canonical_root(root)
        if by_listing is None and any(
            _lexically_holds(spelling, listed) for spelling in (root, str(canonical))
        ):
            by_listing = library
        if by_location is None and target.is_relative_to(canonical):
            by_location = library
    if by_listing is not None and by_location is not None and by_listing is not by_location:
        raise AmbiguousLibrary(path, by_listing, by_location)
    return by_listing or by_location


def _lexically_holds(root: str, listed: str) -> bool:
    """Whether the normalised path *listed* is *root* or spelled under it."""
    spelling = os.path.normpath(os.path.abspath(os.path.expanduser(root)))
    return listed == spelling or listed.startswith(spelling.rstrip(os.sep) + os.sep)


# Each Library root as stored -> its canonical path, for `library_for`, so a
# lookup doesn't resolve every Library's mount. A folder root never changes
# once registered; RomM's may (and so may where its symlink points), so the
# cache is dropped whenever RomM's settings are saved.
_canonical_roots: dict[str, Path] = {}
_canonical_generation: int | None = None


def _canonical_root(root: str) -> Path:
    """*root* resolved, cached until RomM's settings are next saved. Blocking
    the first time; a root that can't be resolved raises and isn't cached."""
    global _canonical_generation
    from services.romm import settings as romm_settings

    generation = romm_settings.save_generation()
    with _lock:
        if _canonical_generation != generation:
            _canonical_roots.clear()
            _canonical_generation = generation
        cached = _canonical_roots.get(root)
    if cached is not None:
        return cached
    canonical = Path(root).resolve()
    with _lock:
        if _canonical_generation == generation:
            _canonical_roots[root] = canonical
    return canonical


def _record_id(library_id: str) -> int | None:
    if not library_id.startswith(ID_PREFIX):
        return None
    try:
        return int(library_id[len(ID_PREFIX):])
    except ValueError:
        return None


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def find_overlap(root: Path, others: list[tuple[str, Path]]) -> tuple[str, Path] | None:
    """The first Library in *others* whose root equals, holds, or sits inside *root*.

    The one non-overlap rule for Library roots, run in both directions:
    registering a folder checks it against every folder Library and RomM's
    root, and saving RomM's root checks it against every folder Library. Two
    overlapping roots would put the same Images under two Rule sets. *others*
    is ``(name, resolved root)`` pairs. Callers hold the registration lock
    (:func:`register`, :func:`romm_root_claim`), so no save races the check.
    """
    for other_name, other_root in others:
        if _overlaps(root, other_root):
            return other_name, other_root
    return None


def _resolved(path: str) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path)


def _romm_root() -> Path | None:
    """RomM's library folder, resolved. Touches the disk (RomM's mount)."""
    from services.romm.library import romm_library

    if not romm_library.root:
        return None
    return _resolved(romm_library.root)


def check_romm_root(path: str) -> None:
    """Refuse RomM's library root at *path* if it overlaps a folder Library.

    Raises :class:`LibraryPathError` (409) naming the Library. Blocking
    (resolves the path); the caller holds the registration lock.
    """
    root = _resolved(path)
    others = [(lib.name, Path(lib.root)) for lib in folder_libraries()]
    hit = find_overlap(root, others)
    if hit is not None:
        other_name, other_root = hit
        raise LibraryPathError(
            409,
            f"RomM's library folder overlaps the folder Library {other_name!r} at "
            f"{other_root}; remove that Library or pick another folder",
        )


async def run_admitted(func, *args, timeout: float, **kwargs):
    """Run a Library folder worker, bounded and admission-controlled.

    Used by registration, the RomM root check and the Platform mapping save
    (#304): each resolves and stats a folder inside a Library.

    The work resolves and stats a folder that may sit on a mount that stopped
    answering, where it blocks for good. It runs on a disposable thread
    (``run_detached``) under *timeout* (``asyncio.TimeoutError`` past it), and
    at most ``MAX_PENDING_WORKERS`` such workers are outstanding at once: a
    slot is held until the worker actually returns, even after its request
    timed out, so retries against a dead mount cannot pile up threads. With
    every slot taken the call is refused at once (``LibraryPathError`` 503)
    and starts nothing.
    """
    if not _worker_slots.acquire(blocking=False):
        raise LibraryPathError(
            503, "Earlier Library checks are still waiting on a folder that isn't "
            "responding. Try again later.",
        )
    # Whoever takes `claim` first owns the slot's release: the worker when it
    # starts (released when it returns), or this awaiter when the worker never
    # started (refused by the probe ceiling, or cancelled before it ran).
    claim = threading.Lock()

    def worker():
        if not claim.acquire(blocking=False):
            return None  # abandoned before it started; the awaiter released
        try:
            return func(*args, **kwargs)
        finally:
            _worker_slots.release()

    try:
        return await asyncio.wait_for(run_detached(worker), timeout)
    except ProbeCapacityExceeded:
        raise LibraryPathError(
            503, "Too many filesystem checks are blocked. Try again later.",
        ) from None
    finally:
        if claim.acquire(blocking=False):
            _worker_slots.release()


@asynccontextmanager
async def romm_root_claim(path: str | None):
    """Hold the registration lock while RomM's library root is checked and saved.

    The same lock :func:`register` takes, so a folder registration cannot
    slip in between this check and the save. A blank *path* (no root, or
    clearing it) overlaps nothing but still takes the lock. Waits at most
    ``REGISTER_WAIT_SECONDS``, polling rather than parking a thread, so a
    cancelled request never leaves the lock taken.

    The check itself is bounded (``ROOT_CHECK_SECONDS``, through
    :func:`run_admitted`): a root on a mount that stopped answering is refused
    with 503 and the lock released, rather than holding it for good. Callers
    take this before any lock of their own, so a hung check holds nothing else.
    """
    deadline = time.monotonic() + REGISTER_WAIT_SECONDS
    while not _register_lock.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise LibraryPathError(
                409, "A Library registration is still running; try again shortly",
            )
        await asyncio.sleep(0.05)
    try:
        if path:
            await run_in_threadpool(_loaded)
            try:
                await run_admitted(check_romm_root, path, timeout=ROOT_CHECK_SECONDS)
            except asyncio.TimeoutError:
                raise LibraryPathError(
                    503, "RomM's library folder isn't responding, so nothing was "
                    "saved. Try again later.",
                ) from None
        yield
    finally:
        _register_lock.release()


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
        hit = find_overlap(root, others)
        if hit is not None:
            other_name, other_root = hit
            if root == other_root:
                # Most often a retry after a 504: the first registration went
                # on and finished.
                raise LibraryPathError(
                    409,
                    f"Already the Library {other_name!r}: an earlier registration "
                    "of this folder has finished",
                )
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


def check_gamelists_folder(path: str) -> str:
    """*path* as an ES-DE gamelists folder (#387): resolved, or LibraryPathError.

    It must be an absolute folder confirmed inside a configured Volume. "Couldn't
    tell" (a Volume root probe failed) is refused too (503), never taken as
    inside. Blocking (resolves and stats); never on the event loop.
    """
    path = path.strip()
    if not os.path.isabs(path):
        raise LibraryPathError(400, "The gamelists folder must be an absolute path")
    inside = volume_containment(path, treat_archives=False)
    if inside is None:
        raise LibraryPathError(
            503, "Couldn't confirm the gamelists folder is inside a Volume (a Volume "
            "isn't answering). Nothing was saved; try again later.",
        )
    if not inside:
        raise LibraryPathError(403, "The gamelists folder must be inside a configured Volume")
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise LibraryPathError(404, "Gamelists folder not found")
    return str(resolved)


async def admitted(func, *args, what: str, **kwargs):
    """Run a blocking Library settings worker that resolves a folder (#387),
    bounded and admission-controlled (:func:`run_admitted`, ``ROOT_CHECK_SECONDS``).
    A worker past the bound is refused with :class:`LibraryPathError` 503
    naming *what* isn't answering."""
    try:
        return await run_admitted(func, *args, timeout=ROOT_CHECK_SECONDS, **kwargs)
    except asyncio.TimeoutError:
        raise LibraryPathError(
            503, f"{what} isn't responding, so nothing was saved. Try again later.",
        ) from None


def update(
    library_id: str, *, name: str | None = None,
    schedule_enabled: bool | None = None, max_per_sweep: int | None = None,
    es_frontend: bool | None = None, gamelists_folder: str | None = None,
    layout: str | None = None,
) -> FolderLibrary | None:
    """Change a folder Library's name or settings. None when there is no such Library.

    Writes the row, then changes the cached Library in place: a walk of it may
    be running (or stuck on a dead mount), and none of these settings needs it.

    *gamelists_folder* (#387) is stored as given; ``""`` clears it. It must
    already have passed :func:`check_gamelists_folder`, which the caller runs
    first, bounded (:func:`admitted`): this only persists, so a check that
    outlasts its bound and is abandoned never saves anything later (Codex
    PRRT_kwDOQ9nULM64Jpv1).
    """
    record_id = _record_id(library_id)
    if record_id is None:
        return None
    library = _loaded().get(record_id)
    if library is None:
        return None
    folder = None if gamelists_folder is None else gamelists_folder.strip()
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
            if es_frontend is not None:
                row.es_frontend = bool(es_frontend)
            if folder is not None:
                row.gamelists_folder = folder or None
            if layout is not None:
                row.layout = layout
            session.commit()
        library.apply_settings(
            name=label, schedule_enabled=schedule_enabled, max_per_sweep=max_per_sweep,
            es_frontend=es_frontend, gamelists_folder=folder, layout=layout,
        )
    return library


async def remove(library_id: str) -> bool:
    """Forget a folder Library, its Rules and its run state. Touches no files."""
    from services import library_records

    record_id = _record_id(library_id)
    library = _loaded().get(record_id) if record_id is not None else None
    if library is None:
        return False
    # Marked before the generation moves: a find-duplicates run that starts
    # from here on is refused, so nothing is written after its records go.
    library_records.begin_removal(library_id)
    try:
        await _remove(library, library_id, record_id)
    finally:
        library_records.end_removal(library_id)
    return True


async def _remove(library: FolderLibrary, library_id: str, record_id: int) -> None:
    from services.rules import sweep as rules_sweep

    await rules_sweep.forget_library(library)

    def _delete() -> None:
        from services import library_records, platform_mappings

        platform_mappings.forget_library(library_id)
        library_records.forget(library_id)
        with _session() as session:
            row = session.get(_db.FolderLibraryRecord, record_id)
            if row is not None:
                session.delete(row)
                session.commit()
        with _lock:
            if _cache is not None:
                _cache.pop(record_id, None)

    await run_in_threadpool(_delete)


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
        # Used by an EmulationStation frontend: conversions update gamelist.xml (#386).
        "es_frontend": library.referrer_enabled(ES_FRONTEND_REFERRER) is True,
        # Its ES-DE gamelists folder, or None (#387).
        "gamelists_folder": (
            library.referrer_options(ES_FRONTEND_REFERRER).get(GAMELISTS_FOLDER) or None
        ),
        # Where each Platform's Images go (#430); None for a Library whose
        # structure is its own (RomM's).
        "layout": getattr(library, "layout", None),
    }


def placements(library: Library) -> list[dict[str, Any]]:
    """Where new Images for every known Platform would land in *library* (#430).

    One row per Platform, sorted by name: the folder as the Library's
    :meth:`~services.rules.library.Library.placement_folders` answers, relative
    to the root, and whether it exists yet (``False``: it will be created).
    Blocking (stats each folder, may ask RomM); run it bounded, off the loop.
    Raises :class:`~services.rules.library.LibraryUnreadable`.
    """
    from services.platform_detect import platforms

    known = platforms()
    folders = library.placement_folders(p["slug"] for p in known)
    root = library.root or ""
    out = []
    for platform in known:
        path = folders.get(platform["slug"])
        if path is None:
            continue
        rel = Path(os.path.relpath(path, root)).as_posix() if root else path
        out.append({
            "slug": platform["slug"], "name": platform["name"], "path": path,
            "folder": "" if rel == "." else rel, "exists": os.path.isdir(path),
        })
    return out
