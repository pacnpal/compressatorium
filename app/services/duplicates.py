"""A Library's find-duplicates run (#388): content keys, groups and Findings.

A run is one external job per Library, like a check Rule's run: it shows in
the Jobs panel, can be cancelled, reports its progress, and is never counted
as a conversion (``ConversionMode.FIND_DUPLICATES`` is an external mode). It:

1. lists every Image of the Library (``Library.all_images``: for a folder
   Library the same walk every listing uses, so junk and RomM's
   ``.romm_tmp_*`` staging are never seen);
2. leaves out an Image a job is using or writing (a lock on it, or an active
   job's Source or output): an output being written is never grouped;
3. reads the content key (:mod:`services.content_keys`) of each Image that has
   none stored for its current size and mtime, one file at a time, each read
   bounded for a slow Volume;
4. groups the Images by key, checks each grouped file is still the one it
   read and is not being written, and records each group of two or more as
   one ``duplicate`` Finding, reconciled against the Library's earlier groups
   so a group that went away is cleared. A stored group with a copy the run
   could not see (in a folder the listing could not open, a file it could not
   read, a file in use or changed while it looked) is kept exactly as it was:
   not seen is not gone;
5. stores what it saw (how many it keyed and read, the formats it could not
   check) for the Findings view.

A cancelled run, or one whose Library was removed meanwhile, writes no
Findings. Cancel is checked once more under the Findings store's write lock,
just before the write: that is the run's commit point. A cancel that arrives
after it is too late to undo the write, so the run reports what it did. Keys
it already read stay stored: they describe their files. Nothing here moves,
renames or deletes a file.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi.concurrency import run_in_threadpool
from logging_setup import get_logger
from models import ConversionMode
from services import content_keys, findings, image_sets
from services import db as _db
from services.content_keys import Identity, KeyUnavailable
from services.job_manager import job_manager
from services.lock_manager import lock_manager
from services.rules.library import Image, ImageListing, Library, LibraryUnreadable
from services.subprocess_runner import (
    ProgressAbandoned,
    ReadCancelled,
    Stalled,
    StorageAbandoned,
    collect_abandonment,
    run_detached,
    run_detached_within_stall_bound,
)
from utils.path_utils import is_within_configured_volumes

logger = get_logger("duplicates")

# How long listing the Library may take, a walk that is already running
# included (it is waited for, then the listing is taken again).
_LIST_SECONDS = 300
_LIST_RETRY_SECONDS = 1.0

# Library id -> the job id of its running find-duplicates run.
_running: dict[str, str] = {}
_tasks: set[asyncio.Task] = set()
# Serialises a run summary's write against forgetting its Library.
_write_lock = threading.Lock()


class LibraryRemoving(Exception):
    """The Library's removal has begun: no run of it may start."""


class AlreadyRunning(Exception):
    """This Library's find-duplicates run is still running."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"a find-duplicates run is still running ({job_id})")
        self.job_id = job_id


@dataclass(frozen=True)
class Candidate:
    """One Image the run may group: a file inside the Volumes, not in use."""

    image_key: str
    path: str
    identity: Identity
    container: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def running_job(library_id: str) -> str | None:
    return _running.get(library_id)


def start(library: Library) -> Any:
    """Start *library*'s find-duplicates run as an external job. Returns the job.

    Raises :class:`AlreadyRunning` while its previous run is still running.
    """
    from services import library_records
    from services.rules.sweep import library_generation

    if library.id in _running:
        raise AlreadyRunning(_running[library.id])
    # Captured under the lock a removal marks the Library with, and refused
    # once that has begun or this Library object is out of date (#388).
    is_current = getattr(library, "is_current", None)
    generation = library_records.capture_unless_removing(
        library.id, lambda: library_generation(library.id),
    )
    if generation is None or (is_current is not None and not is_current()):
        raise LibraryRemoving(f"{library.name} is being removed")
    job = job_manager.create_external_job(
        f"Find duplicates in {library.name}", ConversionMode.FIND_DUPLICATES,
        message="Listing the Library",
    )
    _running[library.id] = job.id
    task = asyncio.get_running_loop().create_task(_run(library, job.id, generation))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def drain() -> None:
    """Wait for every running find-duplicates run (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ----------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------


class _Stopped(Exception):
    """The run was cancelled, or its Library removed: nothing more is written."""


class _Abandoned(Exception):
    """A tool left a child running on storage that stopped answering: the run
    stops, with nothing written."""


async def _run(library: Library, job_id: str, generation: int) -> None:
    from services.rules.sweep import library_generation

    def current() -> bool:
        return library_generation(library.id) == generation

    try:
        summary = await _find(library, job_id, current)
    except _Stopped as stop:
        await job_manager.finish_external_job_cancelled(job_id, message=str(stop))
    except LibraryUnreadable as exc:
        await job_manager.finish_external_job(
            job_id, success=False, error_message=f"The Library could not be listed: {exc}",
        )
    except _Abandoned as exc:
        await job_manager.finish_external_job(job_id, success=False, error_message=str(exc))
    except Exception:
        logger.warning("duplicates: the run for %s failed", library.id, exc_info=True)
        await job_manager.finish_external_job(
            job_id, success=False, error_message="The find-duplicates run stopped on an error",
        )
    else:
        job = job_manager.get_job(job_id)
        if job is not None:
            job.message = describe(summary)
        await job_manager.finish_external_job(job_id, success=True)
    finally:
        if _running.get(library.id) == job_id:
            _running.pop(library.id, None)


def _stop_if_cancelled(job_id: str, current: Callable[[], bool]) -> None:
    if job_manager.is_cancelled(job_id):
        raise _Stopped("Cancelled: no Findings were written")
    if not current():
        raise _Stopped("The Library was removed: no Findings were written")


async def _find(library: Library, job_id: str, current: Callable[[], bool]) -> dict:
    cancel_event = job_manager.get_cancel_event(job_id)
    listing = await _list(library, cancel_event, job_id, current)
    # Never `image.path` here: a Library may resolve it lazily on its mount
    # (RomM's `local_path`), so it is read in `_inspect`, detached and bounded.
    listed = list(listing.images)
    _stop_if_cancelled(job_id, current)
    await job_manager.update_external_job(
        job_id, progress=0, message=f"Looking at {len(listed)} Images",
    )
    # What a job is reading or writing right now, as the queue knows it: an
    # output being written is never grouped. The jobs are snapshot on the loop
    # and their paths (a sheet's tracks) resolved off it, bounded.
    try:
        active = [
            path for _id, paths in await job_manager.active_job_candidates(
                cancel_event=cancel_event, stall=content_keys.STALL_SECONDS,
            )
            for path in paths
        ]
        inspected = await run_detached_within_stall_bound(
            _inspect, listed, active, what=f"looking at {library.name}",
            cancel_event=cancel_event, stall=content_keys.STALL_SECONDS,
            report_progress=True,
        )
    except ReadCancelled:
        raise _Stopped("Cancelled: no Findings were written") from None
    except (OSError, Stalled, ProgressAbandoned) as exc:
        raise LibraryUnreadable(f"the Library's files did not answer: {exc}") from exc
    candidates, in_use, unstatted, (sheet_tracks, sheet_problem) = inspected
    keys = await run_in_threadpool(
        content_keys.stored, {c.path: c.identity for c in candidates},
    )
    missing = [c for c in candidates if c.path not in keys]
    computed = 0
    # Files this run could not read: the walk's, a stat that failed, a key
    # that could not be read. With the folders the walk could not list, what
    # the run did not see.
    unseen = {*listing.unreadable, *unstatted}
    for index, candidate in enumerate(missing):
        _stop_if_cancelled(job_id, current)
        await job_manager.update_external_job(
            job_id, progress=int(95 * index / max(1, len(missing))),
            message=(
                f"Reading {index + 1} of {len(missing)}: {os.path.basename(candidate.path)}"
            ),
        )
        # A child a tool could not kill (it outlived SIGKILL) means the storage
        # is not answering: going on would strand one more per file. The run
        # stops there, as the metadata scan does (#268, Codex 4590926165).
        # Any other failure costs this file only.
        key = after = unavailable = None
        with collect_abandonment() as abandoned:
            try:
                key = await content_keys.build(candidate.path, cancel_event=cancel_event)
                after = await content_keys.read_identity(
                    candidate.path, cancel_event=cancel_event,
                )
            except ReadCancelled:
                raise _Stopped("Cancelled: no Findings were written") from None
            except StorageAbandoned as exc:
                abandoned.append(str(exc))
            except KeyUnavailable as exc:
                unavailable = exc
        # Checked once, right after the read and before anything branches on
        # what it returned (a key, an unavailable key, an exception): a tool
        # that left a child behind may still have answered (Codex 4591089580).
        if abandoned:
            raise _Abandoned(
                f"Reading {os.path.basename(candidate.path)} left a process stuck on "
                f"unresponsive storage ({', '.join(abandoned)}); it is still running. "
                "No Findings were written",
            )
        if unavailable is not None:
            logger.info("duplicates: %s", unavailable)
            unseen.add(candidate.path)
            continue
        if after != candidate.identity:
            # Changed while it was read: the key may describe neither version.
            unseen.add(candidate.path)
            continue
        await run_in_threadpool(
            content_keys.store, candidate.path, candidate.identity, key, candidate.container,
        )
        keys[candidate.path] = content_keys.Stored(key=key, container=candidate.container)
        computed += 1
    _stop_if_cancelled(job_id, current)
    groups = _groups(candidates, keys)
    # The files about to be shown as copies: still the files that were read,
    # and nothing started writing one meanwhile.
    grouped = [c for members in groups.values() for c in members]
    try:
        changed = await run_detached_within_stall_bound(
            _changed, grouped, what=f"looking at {library.name} again",
            cancel_event=cancel_event, stall=content_keys.STALL_SECONDS,
            report_progress=True,
        )
    except ReadCancelled:
        raise _Stopped("Cancelled: no Findings were written") from None
    except (OSError, Stalled, ProgressAbandoned) as exc:
        raise LibraryUnreadable(f"the Library's files did not answer: {exc}") from exc
    in_use = [*in_use, *changed]
    groups = {
        key: kept for key, members in groups.items()
        if len(kept := [c for c in members if c.path not in changed]) >= 2
    }
    # A stored group with a copy the run did not see stays exactly as it was:
    # one it could not read or list, and one in use or changed while it looked.
    held = await run_in_threadpool(
        _held, library.id, {*unseen, *in_use}, listing.unlisted,
    )
    observed = [
        finding_for(key, members) for key, members in sorted(groups.items())
        if key not in held
    ]
    # The commit point, asked under the store's write lock: a cancel or a
    # removed Library that comes while the write waits writes nothing.
    gate = {"open": False}

    def may_write() -> bool:
        gate["open"] = current() and not job_manager.is_cancelled(job_id)
        return gate["open"]

    counts = await run_in_threadpool(
        lambda: findings.reconcile(
            library.id, observed, kinds=(findings.DUPLICATE,),
            keep_under=sorted(held), current=may_write,
        ),
    )
    if not gate["open"]:
        _stop_if_cancelled(job_id, current)
        raise _Stopped("Cancelled: no Findings were written")
    no_key = Counter(
        keys[c.path].container for c in candidates
        if c.path in keys and keys[c.path].key is None
    )
    summary = {
        "images": len(listed),
        "keyed": sum(1 for c in candidates if c.path in keys and keys[c.path].key),
        "computed": computed,
        "groups": len(groups),
        "copies": sum(len(members) for members in groups.values()),
        "not_checked": dict(sorted(no_key.items())),
        "unreadable": len(unseen),
        "unlisted": len(listing.unlisted),
        "held": len(held),
        # Which groups: a held group is not proven by this run, so it is not
        # offered for delete (#389).
        "held_keys": sorted(held),
        # Every sheet's track identities ((st_dev, st_ino), links followed):
        # a copy that is any sheet's track is never deleted, wherever the
        # sheet is (#389). None when a sheet's tracks could not be read: then
        # no delete is offered for this Library.
        "sheet_tracks": None if sheet_tracks is None else sorted(
            [dev, ino] for dev, ino in sheet_tracks
        ),
        # Why the record is incomplete, for the Findings view.
        "sheet_tracks_problem": sheet_problem,
        "busy": len(in_use),
        "findings": counts,
    }
    await run_in_threadpool(_save_run, library.id, summary, current)
    return summary


async def _list(
    library: Library, cancel_event: asyncio.Event | None, job_id: str,
    current: Callable[[], bool],
) -> ImageListing:
    """Every Image of *library* and what the listing could not see, off the
    loop and bounded.

    A walk of the Library that is already running is waited for, then the
    listing is taken again, all within ``_LIST_SECONDS``.
    """
    def work() -> ImageListing:
        return library.all_images()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LIST_SECONDS
    while True:
        try:
            return await asyncio.wait_for(
                run_detached(work, cancel_event=cancel_event),
                max(0.1, deadline - loop.time()),
            )
        except ReadCancelled:
            raise _Stopped("Cancelled: no Findings were written") from None
        except asyncio.TimeoutError:
            raise LibraryUnreadable("listing it did not finish in time") from None
        except LibraryUnreadable as exc:
            if getattr(exc, "code", "") != "listing_in_progress" or loop.time() >= deadline:
                raise
        _stop_if_cancelled(job_id, current)
        await asyncio.sleep(_LIST_RETRY_SECONDS)


def _inspect(
    listed: list[Image], active: list[str], *, progress: Callable[..., None],
) -> tuple[list[Candidate], list[str], list[str], set[tuple[int, int]] | None]:
    """The Images the run may group, the ones in use, the ones it could not
    stat, and every listed sheet's track identities (None when one sheet's
    tracks could not be read). Blocking: run detached under the stall bound.

    Each Image's path is read here, never on the loop: a Library may resolve
    it lazily on its mount (RomM's ``local_path``), and a mount that stops
    answering then ends this step at the bound, or on a cancel. A file
    outside the Volumes, gone, or not a regular file is left out; one a job
    is using or writing (a lock on it, or an active job's path) is in use.
    """
    in_use = set()
    for path in active:
        progress()
        in_use.add(os.path.realpath(path))
    out: list[Candidate] = []
    busy: list[str] = []
    unstatted: list[str] = []
    sheet_tracks: set[tuple[int, int]] | None = set()
    sheet_problem: str | None = None
    for image in listed:
        progress(str(image.key))
        path = image.path
        if not path:
            continue
        image_key = str(image.key)
        progress(path)
        if sheet_tracks is not None and path.lower().endswith(image_sets.SHEET_EXTENSIONS):
            try:
                sheet_tracks |= image_sets.sheet_track_identities(path)
            except image_sets.SheetOwnerUnknown as exc:
                # Incomplete: no delete is offered while it stays so.
                sheet_tracks = None
                sheet_problem = str(exc)
        if not is_within_configured_volumes(path):
            continue
        # Only a file confirmed missing drops a copy. Any other stat failure (a
        # stale handle, an I/O error) is not seen, which keeps its group.
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            unstatted.append(path)
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if os.path.realpath(path) in in_use or lock_manager.is_locked(path):
            busy.append(path)
            continue
        identity = Identity(size=int(st.st_size), mtime_ns=int(st.st_mtime_ns))
        out.append(Candidate(
            image_key=image_key, path=path, identity=identity,
            container=content_keys.container_of(path),
        ))
    return out, busy, unstatted, (sheet_tracks, sheet_problem)


def _held(library_id: str, unseen: set[str], unlisted: Iterable[str]) -> set[str]:
    """The stored groups with a copy the run did not see: a file in *unseen*, or
    under a folder in *unlisted*. Blocking."""
    files = {os.path.normpath(path) for path in unseen}
    folders = tuple(os.path.normpath(folder) for folder in unlisted)

    def not_seen(path: str) -> bool:
        path = os.path.normpath(path)
        return path in files or any(
            path == folder or path.startswith(folder + os.sep) for folder in folders
        )

    return {
        row["image_key"] for row in findings.stored_of(library_id, findings.DUPLICATE)
        if any(
            not_seen(str((copy or {}).get("path") or ""))
            for copy in row["cause"].get(findings.COPIES) or ()
        )
    }


def _changed(candidates: list[Candidate], *, progress: Callable[..., None]) -> set[str]:
    """The paths among *candidates* that changed or are being written now. Blocking."""
    out: set[str] = set()
    for candidate in candidates:
        progress(candidate.path)
        try:
            same = content_keys.identity_of(candidate.path) == candidate.identity
        except OSError:
            same = False
        if not same or lock_manager.is_locked(candidate.path):
            out.add(candidate.path)
    return out


def _groups(
    candidates: Iterable[Candidate], keys: dict[str, content_keys.Stored],
) -> dict[str, list[Candidate]]:
    """Candidates by content key, two or more to a key."""
    by_key: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        stored = keys.get(candidate.path)
        if stored is not None and stored.key:
            by_key.setdefault(stored.key, []).append(candidate)
    return {key: members for key, members in by_key.items() if len(members) >= 2}


def _size(n: int) -> str:
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def finding_for(key: str, members: list[Candidate]) -> findings.Observed:
    """The ``duplicate`` Finding for one group: every copy's path, container,
    size and mtime. Size and mtime are the identity its key was read for: a
    delete (#389) refuses a copy that no longer has them."""
    return finding_from_copies(key, [
        {
            "image_key": c.image_key, "path": c.path, "container": c.container,
            "size": c.identity.size, "mtime_ns": c.identity.mtime_ns,
        }
        for c in members
    ])


def finding_from_copies(key: str, copies: list[dict]) -> findings.Observed:
    """The ``duplicate`` Finding for *copies* as a Finding's cause lists them."""
    copies = sorted((dict(c) for c in copies), key=lambda c: c["path"])
    containers = ", ".join(sorted({c["container"] for c in copies}))
    total = sum(c["size"] for c in copies)
    return findings.Observed(
        kind=findings.DUPLICATE, image_key=key, path=copies[0]["path"],
        reason=f"{len(copies)} copies of the same disc ({containers}), {_size(total)} in all",
        cause={"key": key, findings.COPIES: copies},
    )


def describe(summary: dict) -> str:
    """A run's summary in one line, for its job."""
    groups = summary.get("groups", 0)
    text = (
        f"{groups} group{'s' if groups != 1 else ''} of duplicates; "
        f"{summary.get('keyed', 0)} Images keyed ({summary.get('computed', 0)} read now)"
    )
    not_checked = sum((summary.get("not_checked") or {}).values())
    if not_checked:
        text += f", {not_checked} not checked"
    if summary.get("unreadable"):
        text += f", {summary['unreadable']} could not be read"
    if summary.get("unlisted"):
        text += f", {summary['unlisted']} folders could not be listed"
    if summary.get("held"):
        text += f", {summary['held']} groups kept as they were"
    if summary.get("busy"):
        text += f", {summary['busy']} in use"
    return text


# ----------------------------------------------------------------------
# the last run per Library
# ----------------------------------------------------------------------


def _session():
    if _db.SessionLocal is None:
        raise RuntimeError("duplicates: db.init_engine() has not run")
    return _db.SessionLocal()


def _save_run(library_id: str, summary: dict, current: Callable[[], bool]) -> None:
    """Store *library_id*'s last run. Blocking. Nothing once its Library is removed."""
    with _write_lock, _session() as session:
        if not current():
            return
        row = session.get(_db.DuplicateRunRecord, library_id)
        if row is None:
            row = _db.DuplicateRunRecord(library_id=library_id)
            session.add(row)
        row.finished_at = _now()
        row.summary = dict(summary)
        session.commit()


def list_runs() -> list[dict[str, Any]]:
    """Every Library's last find-duplicates run. Blocking."""
    with _session() as session:
        rows = session.query(_db.DuplicateRunRecord).order_by(
            _db.DuplicateRunRecord.library_id,
        ).all()
        return [
            {**dict(row.summary or {}), "library_id": row.library_id,
             "finished_at": row.finished_at}
            for row in rows
        ]


def forget_library(library_id: str) -> None:
    """Drop a Library's last run. Blocking. Called through
    :func:`services.library_records.forget`, never on its own."""
    with _write_lock, _session() as session:
        session.query(_db.DuplicateRunRecord).filter_by(library_id=library_id).delete()
        session.commit()
