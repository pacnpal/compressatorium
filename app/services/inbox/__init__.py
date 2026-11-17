"""Inboxes (#431): folders that feed one Library each.

An Inbox is a row of the ``inboxes`` table, loaded once and kept in memory
like the folder Libraries (:mod:`services.libraries`). What happens to the
files dropped in one is :mod:`services.inbox.intake`, which the Rule
scheduler's tick runs.

Saving checks the folder fail-closed: it must be confirmed inside a Volume
(``volume_containment(...) is True``; "couldn't tell" is a 503), a real
folder, not inside any Library's root and not holding one (intake would read
its own output, or move another Library's Images to ``processed/``), and not
the folder of another Inbox, nor inside or around one (two Inboxes would take
the same file). The check and the insert
happen under one lock, and the database's unique folder is the backstop.
Removing an Inbox drops its row, its Findings and its intake state; it never
touches a file.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import db as _db
from sqlalchemy.exc import IntegrityError
from utils.path_utils import volume_containment

__all__ = [
    "DEFAULT_SETTLE_SECONDS",
    "PROCESSED",
    "Inbox",
    "InboxError",
    "all_inboxes",
    "describe",
    "for_library",
    "forget_library",
    "get",
    "holding",
    "note_processed",
    "register",
    "remove",
    "update",
]

# The folder inside an Inbox its originals are moved to. Intake never reads it.
PROCESSED = "processed"
# How long a file's size and mtime must stay the same before it is taken.
DEFAULT_SETTLE_SECONDS = 60
MAX_SETTLE_SECONDS = 7 * 24 * 3600

# Inboxes by id. None until first read. Guards nothing but the cache, so it is
# never held across disk or database access.
_cache: dict[int, Inbox] | None = None
_lock = threading.Lock()
# How long a save waits for another registration (an Inbox's, a Library's or
# RomM's root, which share `libraries.registration_lock`) to finish.
_REGISTER_WAIT_SECONDS = 5.0


class InboxError(ValueError):
    """An Inbox can't be saved. ``status`` is the HTTP status that fits."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Inbox:
    """One Inbox. ``folder`` is resolved; ``library_id`` names the Library it feeds."""

    id: int
    folder: str
    library_id: str
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    processed_count: int = 0

    @property
    def processed_folder(self) -> str:
        return os.path.join(self.folder, PROCESSED)

    def image_key(self, path: str) -> str:
        """The Findings key of the file at *path* in this Inbox."""
        rel = os.path.relpath(path, self.folder).replace(os.sep, "/")
        return f"{self.key_prefix}/{rel}"

    @property
    def key_prefix(self) -> str:
        return f"inbox:{self.id}"


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("inbox: db.init_engine() has not run")
    return _db.SessionLocal()


def _from_row(row: _db.InboxRecord) -> Inbox:
    return Inbox(
        id=row.id, folder=row.folder, library_id=row.library_id,
        settle_seconds=int(row.settle_seconds), processed_count=int(row.processed_count),
    )


def _loaded() -> dict[int, Inbox]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
    with _session() as session:
        rows = session.query(_db.InboxRecord).order_by(_db.InboxRecord.id).all()
        loaded = {row.id: _from_row(row) for row in rows}
    with _lock:
        if _cache is None:
            _cache = loaded
        return _cache


def all_inboxes() -> list[Inbox]:
    """Every Inbox, in the order they were added. Blocking on first use."""
    return list(_loaded().values())


def get(inbox_id: int) -> Inbox | None:
    return _loaded().get(inbox_id)


def for_library(library_id: str) -> list[Inbox]:
    return [inbox for inbox in all_inboxes() if inbox.library_id == library_id]


def _settle(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise InboxError(422, "The settle time must be a whole number of seconds") from None
    if seconds < 0 or seconds > MAX_SETTLE_SECONDS:
        raise InboxError(
            422, f"The settle time must be between 0 and {MAX_SETTLE_SECONDS} seconds",
        )
    return seconds


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _checked_folder(path: str) -> Path:
    """*path* as an Inbox folder: absolute, confirmed inside a Volume, a real
    folder, resolved. Raises :class:`InboxError`. Blocking."""
    path = (path or "").strip()
    if not os.path.isabs(path):
        raise InboxError(400, "The Inbox folder must be an absolute path")
    inside = volume_containment(path, treat_archives=False)
    if inside is None:
        raise InboxError(
            503, "Couldn't confirm the folder is inside a Volume (a Volume isn't "
            "answering). Nothing was saved; try again later.",
        )
    if not inside:
        raise InboxError(403, "The Inbox folder must be inside a configured Volume")
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise InboxError(404, "Folder not found")
    return folder


def overlap_problem(folder: Path, root: Path, name: str, *, own: bool) -> str | None:
    """Why an Inbox at *folder* can't sit with the Library *name* rooted at
    *root*, or None. Both resolved. Intake moves what it takes to
    ``processed/``, so an Inbox inside or around any Library would take that
    Library's Images, or its own output."""
    which = "its Library" if own else "the Library"
    if folder == root or folder.is_relative_to(root):
        return f"The Inbox can't be inside {which} {name!r}"
    if root.is_relative_to(folder):
        return f"The Inbox can't hold {which} {name!r}"
    return None


def _check_library(folder: Path, library_id: str) -> None:
    """Refuse *folder* for the Library *library_id*: unknown, no folder here,
    or overlapping its root or any other Library's. Blocking (resolves roots)."""
    from services import libraries

    library = libraries.get(library_id)
    if library is None:
        raise InboxError(404, "Library not found")
    if not library.root:
        raise InboxError(409, f"The Library {library.name!r} has no folder here yet")
    for other in libraries.all_libraries():
        if not other.root:
            continue
        problem = overlap_problem(
            folder, Path(other.root).resolve(), other.name, own=other.id == library.id,
        )
        if problem:
            raise InboxError(409, problem)


def holding(path: Path) -> Inbox | None:
    """The Inbox whose folder is, holds or sits inside *path* (resolved), if
    any: a Library can't be registered there (#431)."""
    for inbox in all_inboxes():
        if _overlaps(path, Path(inbox.folder)):
            return inbox
    return None


def register(
    path: str, library_id: str, settle_seconds: Any = DEFAULT_SETTLE_SECONDS,
) -> Inbox:
    """Save the folder at *path* as an Inbox for *library_id*. Blocking."""
    from services import libraries

    seconds = _settle(settle_seconds)
    folder = _checked_folder(path)
    _loaded()
    # The Library overlap checks and the insert run under the lock a Library's
    # registration and RomM's root save take too, so a Library registered in
    # or around this folder meanwhile can't slip past either check.
    try:
        with libraries.registration_lock(timeout=_REGISTER_WAIT_SECONDS):
            return _insert(folder, library_id, seconds)
    except libraries.RegistrationBusy:
        raise InboxError(
            409, "Another registration is still running; try again shortly",
        ) from None


def _insert(folder: Path, library_id: str, seconds: int) -> Inbox:
    """Check *folder* against every Library and Inbox, then store it. Called
    with ``libraries.registration_lock`` held. Blocking."""
    _check_library(folder, library_id)
    for other in all_inboxes():
        if _overlaps(folder, Path(other.folder)):
            if str(folder) == other.folder:
                raise InboxError(409, f"{folder} is already an Inbox")
            raise InboxError(409, f"Overlaps the Inbox at {other.folder}")
    with _session() as session:
        row = _db.InboxRecord(
            folder=str(folder), library_id=library_id, settle_seconds=seconds,
            processed_count=0,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise InboxError(409, f"{folder} is already an Inbox") from None
        inbox = _from_row(row)
    with _lock:
        if _cache is not None:
            _cache[inbox.id] = inbox
    return inbox


def update(inbox_id: int, *, settle_seconds: Any = None) -> Inbox | None:
    """Change an Inbox's settle time. None when there is no such Inbox. Blocking."""
    inbox = get(inbox_id)
    if inbox is None:
        return None
    if settle_seconds is None:
        return inbox
    seconds = _settle(settle_seconds)
    with _session() as session:
        row = session.get(_db.InboxRecord, inbox_id)
        if row is None:
            return None
        row.settle_seconds = seconds
        session.commit()
    return _replace(inbox_id, settle_seconds=seconds)


def note_processed(inbox_id: int, count: int = 1) -> None:
    """Count *count* more Images placed by the Inbox. Blocking."""
    with _session() as session:
        row = session.get(_db.InboxRecord, inbox_id)
        if row is None:
            return
        row.processed_count = int(row.processed_count) + count
        session.commit()
        total = int(row.processed_count)
    _replace(inbox_id, processed_count=total)


def _replace(inbox_id: int, **changes: Any) -> Inbox | None:
    with _lock:
        if _cache is None or inbox_id not in _cache:
            return None
        _cache[inbox_id] = replace(_cache[inbox_id], **changes)
        return _cache[inbox_id]


def remove(inbox_id: int) -> bool:
    """Forget an Inbox, its Findings and its intake state. Touches no file. Blocking."""
    from services import findings
    from services.inbox import intake

    inbox = get(inbox_id)
    if inbox is None:
        return False
    with _session() as session:
        row = session.get(_db.InboxRecord, inbox_id)
        if row is not None:
            session.delete(row)
            session.commit()
    with _lock:
        if _cache is not None:
            _cache.pop(inbox_id, None)
    intake.forget(inbox_id)
    findings.forget_under(inbox.library_id, inbox.key_prefix, findings.INBOX_KINDS)
    return True


def forget_library(library_id: str) -> None:
    """Remove every Inbox of a removed Library. Blocking."""
    for inbox in for_library(library_id):
        remove(inbox.id)


def describe(inbox: Inbox) -> dict[str, Any]:
    """An Inbox as the API lists it, with its intake status."""
    from services.inbox import intake

    return {
        "id": inbox.id,
        "folder": inbox.folder,
        "library_id": inbox.library_id,
        "settle_seconds": inbox.settle_seconds,
        "processed": inbox.processed_count,
        **intake.status(inbox.id),
    }
